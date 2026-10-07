"""Verification suite (analytic dam-break tests) and validation record.

Verification asks whether the installed model codes solve their equations correctly on this
computer; validation asks whether a model of a real dam agrees with an accepted study of that
dam. This module writes documented evidence of both for a dam-safety reviewer:

* `run_suite` runs small synthetic problems with known answers through LISFLOOD-FP (and ANUGA
  when its interpreter is given) the way the plugin runs them (`lisflood.write_inputs` and
  `lisflood.run`; `anuga_model.write_job` and the ANUGA worker), compares the results with the
  known solutions and writes into the output folder: `plots/<case>.svg`,
  `Verification_Summary.md` and `.html`, `Verification_Metrics.csv`,
  `Verification_Results.json`, `Verification Record.docx`, and the model files of every run
  under `work/` (the record of exactly what was run).
* `validation_record` turns a benchmark folder written by `benchmark.write_outputs` (this tool
  against an approved HEC-RAS or other study) into a Word 'Validation Record'.

The cases (`CASE_IDS`):

  ritter         LISFLOOD-FP, Ritter (1892) dam break on a dry frictionless bed
  stoker         LISFLOOD-FP, Stoker (1957) dam break on a wet bed, h1/h0 = 0.1, 0.25, 0.5
  uniform        LISFLOOD-FP, steady uniform flow, Manning normal depth (mild slope)
  mass           LISFLOOD-FP, volume balance of a breach-like hydrograph on a synthetic valley
  radial         LISFLOOD-FP, spreading from a point source on a flat plane (grid orientation)
  anuga_ritter   ANUGA, Ritter dam break (through the worker's maximum-depth envelope)
  anuga_uniform  ANUGA, steady uniform flow (steep slope; see the case for why)
  anuga_mass     ANUGA, volume balance on the synthetic valley

Every tolerance is this tool's acceptance criterion, stated with its basis in the record; none
is a regulatory standard. Criteria are of three kinds: 'accuracy' where the scheme is expected
to reproduce the solution; 'conservation' for volume; and 'expected behaviour' bounds where the
literature says the scheme cannot reproduce the solution (the local inertial solver in
supercritical flow: Neal et al. 2012; de Almeida & Bates 2013). Those bounds detect a broken or
different build and quantify the limitation; they do not claim accuracy.

Notes on the ANUGA cases. The worker has no initial-condition-only mode, but a zero hydrograph at
an inlet placed in still water far upstream works: ANUGA's Inlet_operator with zero volume only
levels the (already level) water in the inlet triangles. The worker returns envelopes, not
snapshots; downstream of the dam the Ritter depth only rises with time, so the maximum-depth
envelope there equals the depth at the end of the run. The worker puts a transmissive boundary
on the whole mesh outline, which exchanges water through the sides of a narrow channel (1 to 3
% of the volume in a 40 s dam break), so the channels here are walled by high ground inside the
mesh, and a refined-region polygon along the walls makes them mesh edges (without it the
triangles straddling the wall make the bed jagged).

Python 3.9 compatible; numpy and the standard library only (scipy is not needed).
"""
import csv
import datetime
import hashlib
import html
import json
import math
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import traceback

import numpy as np

from .grid import Grid
from . import lisflood as lf
from .lisflood import ModelError, Cancelled

PASS, FAIL, SKIP, ERROR = "PASS", "FAIL", "SKIP", "ERROR"
STATUSES = (PASS, FAIL, SKIP, ERROR)
G_LISFLOOD = 9.80655        # LISFLOOD-FP's g (lisflood.cpp, ParSolver.g)
G_ANUGA = 9.8               # anuga.config.g

LISFLOOD_CASES = ("ritter", "stoker", "uniform", "mass", "radial")
ANUGA_CASES = ("anuga_ritter", "anuga_uniform", "anuga_mass")
CASE_IDS = LISFLOOD_CASES + ANUGA_CASES

WALL_M = 10.0               # walls: this far above the highest bed
STATUS_SHADES = {PASS: "E2EFDA", FAIL: "F8CBAD", SKIP: "EDEDED", ERROR: "F8CBAD"}
KIND_LABELS = {"accuracy": "accuracy", "conservation": "conservation",
               "bound": "expected behaviour"}

REFS = {
    "ritter": "Ritter, A. (1892). Die Fortpflanzung der Wasserwellen. Zeitschrift des Vereines "
              "Deutscher Ingenieure 36(33), 947-954.",
    "stoker": "Stoker, J.J. (1957). Water Waves: The Mathematical Theory with Applications. "
              "Interscience, New York.",
    "swashes": "Delestre, O., Lucas, C., Ksinant, P.-A., Darboux, F., Laguerre, C., Vo, T.N.T., "
               "James, F., Cordier, S. (2013). SWASHES: a compilation of shallow water analytic "
               "solutions for hydraulic and environmental studies. International Journal for "
               "Numerical Methods in Fluids 72(3), 269-300. doi:10.1002/fld.3741",
    "manning": "Manning, R. (1891). On the flow of water in open channels and pipes. "
               "Transactions of the Institution of Civil Engineers of Ireland 20, 161-207.",
    "chow": "Chow, V.T. (1959). Open-Channel Hydraulics. McGraw-Hill, New York.",
    "bates2010": "Bates, P.D., Horritt, M.S., Fewtrell, T.J. (2010). A simple inertial "
                 "formulation of the shallow water equations for efficient two-dimensional "
                 "flood inundation modelling. Journal of Hydrology 387(1-2), 33-45.",
    "dealmeida2013": "de Almeida, G.A.M., Bates, P. (2013). Applicability of the local inertial "
                     "approximation of the shallow water equations to flood modeling. Water "
                     "Resources Research 49(8), 4833-4844. doi:10.1002/wrcr.20366",
    "neal2012": "Neal, J., Villanueva, I., Wright, N., Willis, T., Fewtrell, T., Bates, P. "
                "(2012). How much physical complexity is needed to model flood inundation? "
                "Hydrological Processes 26(15), 2264-2282. doi:10.1002/hyp.8339",
    "anuga": "Roberts, S. et al. ANUGA: open-source hydrodynamic modelling, Geoscience "
             "Australia / Australian National University (user manual and source code).",
    "bates_deroo": "Bates, P.D., De Roo, A.P.J. (2000). A simple raster-based model for flood "
                   "inundation simulation. Journal of Hydrology 236(1-2), 54-77.",
}

# Expected-behaviour basis shared by the local-inertial dam-break cases.
LIA_BASIS = ("LISFLOOD-FP's acceleration solver is the local inertial approximation (Bates et "
             "al. 2010): it drops the advection term of the momentum equation. de Almeida & "
             "Bates (2013) find good agreement with the full equations 'especially in the lower "
             "range of subcritical flows (Fr < 0.5)', and Neal et al. (2012) that the simpler "
             "models 'were unable to simulate supercritical flows accurately'.")

CASE_INFO = {
    "ritter": ("Ritter dam break on a dry bed", "LISFLOOD-FP"),
    "stoker": ("Stoker dam break on a wet bed", "LISFLOOD-FP"),
    "uniform": ("Steady uniform flow (Manning normal depth)", "LISFLOOD-FP"),
    "mass": ("Volume balance of a breach flood on a synthetic valley", "LISFLOOD-FP"),
    "radial": ("Radial spreading from a point source (grid orientation)", "LISFLOOD-FP"),
    "anuga_ritter": ("Ritter dam break on a dry bed", "ANUGA"),
    "anuga_uniform": ("Steady uniform flow (Manning normal depth)", "ANUGA"),
    "anuga_mass": ("Volume balance of a breach flood on a synthetic valley", "ANUGA"),
}


# ======================================================================================
# analytic solutions
# ======================================================================================
def ritter_depth(x, t, h0, g=G_LISFLOOD):
    """Ritter (1892): depth at x (m from the dam, reservoir at x < 0) and t (s) after an
    instantaneous removal of a dam holding h0 over a dry, horizontal, frictionless bed:
    h = (2 sqrt(g h0) - x/t)^2 / (9 g) for -sqrt(g h0) t <= x <= 2 sqrt(g h0) t."""
    x = np.asarray(x, dtype=float)
    if t <= 0:
        return np.where(x < 0, float(h0), 0.0)
    c0 = math.sqrt(g * h0)
    xi = x / float(t)
    fan = (2.0 * c0 - xi) ** 2 / (9.0 * g)
    return np.where(xi <= -c0, float(h0), np.where(xi >= 2.0 * c0, 0.0, fan))


def ritter_velocity(x, t, h0, g=G_LISFLOOD):
    """u = 2/3 (sqrt(g h0) + x/t) in the rarefaction, 0 elsewhere."""
    x = np.asarray(x, dtype=float)
    if t <= 0:
        return np.zeros_like(x)
    c0 = math.sqrt(g * h0)
    xi = x / float(t)
    return np.where((xi > -c0) & (xi < 2.0 * c0), 2.0 / 3.0 * (c0 + xi), 0.0)


def ritter_front(t, h0, depth=0.0, g=G_LISFLOOD):
    """Position of the point where Ritter's depth equals `depth` (0 = the wet front at
    2 sqrt(g h0) t)."""
    return float(t) * (2.0 * math.sqrt(g * h0) - 3.0 * math.sqrt(g * max(depth, 0.0)))


def stoker_state(h0, h1, g=G_LISFLOOD, tol=1e-14):
    """Middle state of Stoker's (1957) wet-bed dam break (h0 upstream, h1 < h0 at rest
    downstream, horizontal frictionless bed): a rarefaction from the reservoir to the
    constant state (h2, u2), then a bore moving at s into the still water.

    h2 is found by bisection on the two conditions for u2:
      Riemann invariant across the rarefaction:  u2 + 2 sqrt(g h2) = 2 sqrt(g h0)
      Rankine-Hugoniot across the bore (still water ahead):
          s = sqrt(g h2 (h1 + h2) / (2 h1)),  u2 = s (1 - h1 / h2)
    Returns a dict: h2, u2, c2, s (bore speed), fan_tail (speed of the rarefaction's tail,
    u2 - c2), froude2 (u2 / c2)."""
    h0, h1 = float(h0), float(h1)
    if not (h0 > 0.0 and 0.0 < h1 < h0):
        raise ValueError("Stoker's solution needs 0 < h1 < h0 (dry bed: use Ritter's)")
    c0 = math.sqrt(g * h0)

    def bore_u(h2):
        return math.sqrt(g * h2 * (h1 + h2) / (2.0 * h1)) * (1.0 - h1 / h2)

    def f(h2):
        return bore_u(h2) - 2.0 * (c0 - math.sqrt(g * h2))

    a, b = h1, h0                    # f(h1) < 0 < f(h0)
    for _ in range(300):
        m = 0.5 * (a + b)
        if f(m) > 0.0:
            b = m
        else:
            a = m
        if b - a <= tol * h0:
            break
    h2 = 0.5 * (a + b)
    c2 = math.sqrt(g * h2)
    u2 = 2.0 * (c0 - c2)
    s = math.sqrt(g * h2 * (h1 + h2) / (2.0 * h1))
    return {"h0": h0, "h1": h1, "h2": h2, "u2": u2, "c2": c2, "s": s, "fan_tail": u2 - c2,
            "froude2": u2 / c2, "g": g}


def stoker_depth(x, t, h0, h1, g=G_LISFLOOD):
    """Depth of Stoker's wet-bed dam break at x (m from the dam) and t (s)."""
    x = np.asarray(x, dtype=float)
    if t <= 0:
        return np.where(x < 0, float(h0), float(h1))
    st = stoker_state(h0, h1, g)
    c0 = math.sqrt(g * h0)
    xi = x / float(t)
    fan = (2.0 * c0 - xi) ** 2 / (9.0 * g)
    return np.where(xi <= -c0, h0, np.where(xi <= st["fan_tail"], fan,
                                            np.where(xi <= st["s"], st["h2"], h1)))


def stoker_velocity(x, t, h0, h1, g=G_LISFLOOD):
    x = np.asarray(x, dtype=float)
    if t <= 0:
        return np.zeros_like(x)
    st = stoker_state(h0, h1, g)
    c0 = math.sqrt(g * h0)
    xi = x / float(t)
    return np.where(xi <= -c0, 0.0, np.where(xi <= st["fan_tail"], 2.0 / 3.0 * (c0 + xi),
                                             np.where(xi <= st["s"], st["u2"], 0.0)))


def manning_normal_depth(q, n, slope):
    """Normal depth (m) for a discharge per unit width q (m2/s) with the hydraulic radius
    equal to the depth (wide channel; the friction law of 2D depth-averaged models, which have
    no side-wall friction): q = h^(5/3) S^(1/2) / n, so h = (q n / S^(1/2))^(3/5)."""
    if q <= 0 or n <= 0 or slope <= 0:
        raise ValueError("q, n and the slope must be positive")
    return (q * n / math.sqrt(slope)) ** 0.6


def manning_normal_depth_rect(Q, b, n, slope):
    """Normal depth (m) of a rectangular channel of width b with its side walls:
    Q = (1/n) A R^(2/3) S^(1/2), A = b y, R = b y / (b + 2 y). Solved by bisection."""
    if Q <= 0 or b <= 0 or n <= 0 or slope <= 0:
        raise ValueError("Q, b, n and the slope must be positive")

    def q_of(y):
        a = b * y
        return a * (a / (b + 2.0 * y)) ** (2.0 / 3.0) * math.sqrt(slope) / n

    lo, hi = 0.0, max(1.0, manning_normal_depth(Q / b, n, slope))
    while q_of(hi) < Q:
        hi *= 2.0
    for _ in range(200):
        m = 0.5 * (lo + hi)
        if q_of(m) < Q:
            lo = m
        else:
            hi = m
        if hi - lo < 1e-13 * hi:
            break
    return 0.5 * (lo + hi)


def _trapz(y, x):
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return 0.0
    return float(np.sum(0.5 * (y[1:] + y[:-1]) * np.diff(x)))


def _front_position(x, h, thr):
    """Furthest x where the depth reaches `thr`, interpolated between the last cell at or
    above it and the next one."""
    idx = np.nonzero(np.asarray(h) >= thr)[0]
    if not len(idx):
        return float("nan")
    i = int(idx[-1])
    if i + 1 >= len(x):
        return float(x[i])
    a, b = float(h[i]), float(h[i + 1])
    f = (a - thr) / (a - b) if a != b else 0.0
    return float(x[i] + max(0.0, min(1.0, f)) * (x[i + 1] - x[i]))


# ======================================================================================
# a small SVG line plot (numbers on the axes with the decimals they need)
# ======================================================================================
PALETTE = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b", "#17becf")


def _nice_step(span, n):
    if not span > 0:
        return 1.0
    raw = span / float(n)
    mag = 10.0 ** math.floor(math.log10(raw))
    for m in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= m * mag * (1 + 1e-9):
            return m * mag
    return 10.0 * mag


def _decimals(step):
    d = 0
    while d < 8 and abs(step * 10 ** d - round(step * 10 ** d)) > 1e-6 * max(1.0, step * 10 ** d):
        d += 1
    return d


def _ticks(lo, hi, n):
    step = _nice_step(hi - lo, n)
    k0 = int(math.ceil(lo / step - 1e-9))
    k1 = int(math.floor(hi / step + 1e-9))
    return [k * step for k in range(k0, k1 + 1)][:60], _decimals(step)


def _num_text(v, dec):
    s = "{:,.{}f}".format(v, dec)
    return "0" if s in ("-0", "-0.0", "-0.00", "-0.000") or re.match(r"^-0\.?0*$", s) else s


def plot_svg(series, title, xlabel, ylabel, width=760, height=380, xlim=None, ylim=None,
             include_zero=True, vlines=None):
    """A line plot. `series`: [{"label", "x", "y", "color", "dash" (bool), "marker" (bool),
    "width"}]; NaN breaks a line. `vlines`: [(x, label)]. Returns the SVG text."""
    left, right, top, bottom = 72, 20, 46, 50
    pw, ph = width - left - right, height - top - bottom
    fx, fy = [], []
    for s in series:
        x = np.asarray(s["x"], dtype=float)
        y = np.asarray(s["y"], dtype=float)
        ok = np.isfinite(x) & np.isfinite(y)
        fx.append(x[ok])
        fy.append(y[ok])
    allx = np.concatenate(fx) if fx else np.zeros(0)
    ally = np.concatenate(fy) if fy else np.zeros(0)
    if xlim:
        x0, x1 = float(xlim[0]), float(xlim[1])
    else:
        x0, x1 = (float(allx.min()), float(allx.max())) if allx.size else (0.0, 1.0)
    if ylim:
        y0, y1 = float(ylim[0]), float(ylim[1])
    else:
        y0, y1 = (float(ally.min()), float(ally.max())) if ally.size else (0.0, 1.0)
        if include_zero:
            y0, y1 = min(y0, 0.0), max(y1, 0.0)
        y1 = y1 + 0.06 * ((y1 - y0) or abs(y1) or 1.0)
    if not x1 > x0:
        x1 = x0 + 1.0
    if not y1 > y0:
        y1 = y0 + 1.0
    xt, xdec = _ticks(x0, x1, 7)
    yt, ydec = _ticks(y0, y1, 5)

    def px(v):
        return left + (v - x0) / (x1 - x0) * pw

    def py(v):
        return top + (1.0 - (v - y0) / (y1 - y0)) * ph

    esc = html.escape
    out = ['<rect width="100%" height="100%" fill="#ffffff"/>',
           '<defs><clipPath id="plotarea"><rect x="{}" y="{}" width="{}" height="{}"/>'
           '</clipPath></defs>'.format(left, top, pw, ph),
           '<text x="{}" y="24" font-size="14" font-weight="bold">{}</text>'.format(
               left, esc(title))]
    for v in yt:
        out.append('<line x1="{0}" y1="{1:.1f}" x2="{2}" y2="{1:.1f}" stroke="#e3e3e3"/>'
                   '<text x="{3}" y="{4:.1f}" font-size="10" text-anchor="end">{5}</text>'
                   .format(left, py(v), left + pw, left - 6, py(v) + 3.5, _num_text(v, ydec)))
    for v in xt:
        out.append('<line x1="{0:.1f}" y1="{1}" x2="{0:.1f}" y2="{2}" stroke="#eeeeee"/>'
                   '<text x="{0:.1f}" y="{3}" font-size="10" text-anchor="middle">{4}</text>'
                   .format(px(v), top, top + ph, top + ph + 14, _num_text(v, xdec)))
    for xv, lab in (vlines or []):
        if x0 <= xv <= x1:
            out.append('<line x1="{0:.1f}" y1="{1}" x2="{0:.1f}" y2="{2}" stroke="#777777" '
                       'stroke-dasharray="3,3"/><text x="{3:.1f}" y="{4}" font-size="10" '
                       'fill="#555555">{5}</text>'.format(px(xv), top, top + ph, px(xv) + 3,
                                                          top + 12, esc(lab)))
    for k, s in enumerate(series):
        colr = s.get("color") or PALETTE[k % len(PALETTE)]
        x = np.asarray(s["x"], dtype=float)
        y = np.asarray(s["y"], dtype=float)
        ok = np.isfinite(x) & np.isfinite(y)
        dash = ' stroke-dasharray="6,4"' if s.get("dash") else ""
        if not s.get("no_line"):
            start = None
            for i in range(len(x) + 1):
                good = i < len(x) and ok[i]
                if good and start is None:
                    start = i
                if not good and start is not None:
                    pts = " ".join("{:.1f},{:.1f}".format(px(a), py(b))
                                   for a, b in zip(x[start:i], y[start:i]))
                    if i - start >= 2:
                        out.append('<polyline fill="none" stroke="{}" stroke-width="{}"{} '
                                   'clip-path="url(#plotarea)" points="{}"/>'.format(
                                       colr, s.get("width", 1.8), dash, pts))
                    start = None
        if s.get("marker"):
            idx = np.nonzero(ok)[0]
            if len(idx) > 150:
                idx = idx[::int(math.ceil(len(idx) / 150.0))]
            for i in idx:
                if x0 <= x[i] <= x1 and y0 <= y[i] <= y1:
                    out.append('<circle cx="{:.1f}" cy="{:.1f}" r="2.6" fill="{}"/>'.format(
                        px(x[i]), py(y[i]), colr))
    # legend (top right, inside the plot)
    labels = [(s.get("label") or "", s.get("color") or PALETTE[k % len(PALETTE)], s)
              for k, s in enumerate(series) if s.get("label")]
    if labels:
        lw = min(pw - 10, 34 + 6.3 * max(len(t) for t, _c, _s in labels))
        lx, ly = left + pw - lw - 6, top + 6
        out.append('<rect x="{:.1f}" y="{}" width="{:.1f}" height="{}" fill="#ffffff" '
                   'fill-opacity="0.9" stroke="#bbbbbb"/>'.format(lx, ly, lw,
                                                                   8 + 15 * len(labels)))
        for i, (t, colr, s) in enumerate(labels):
            yy = ly + 13 + 15 * i
            if not s.get("no_line"):
                out.append('<line x1="{:.1f}" y1="{}" x2="{:.1f}" y2="{}" stroke="{}" '
                           'stroke-width="2"{}/>'.format(
                               lx + 6, yy - 4, lx + 26, yy - 4, colr,
                               ' stroke-dasharray="6,4"' if s.get("dash") else ""))
            if s.get("marker"):
                out.append('<circle cx="{:.1f}" cy="{}" r="2.6" fill="{}"/>'.format(
                    lx + 16, yy - 4, colr))
            out.append('<text x="{:.1f}" y="{}" font-size="10.5">{}</text>'.format(
                lx + 31, yy, esc(t)))
    out.append('<rect x="{}" y="{}" width="{}" height="{}" fill="none" stroke="#888888"/>'
               .format(left, top, pw, ph))
    out.append('<text x="{}" y="{}" font-size="11" text-anchor="middle">{}</text>'.format(
        left + pw / 2.0, height - 10, esc(xlabel)))
    out.append('<text x="16" y="{0}" font-size="11" text-anchor="middle" transform="rotate(-90 '
               '16 {0})">{1}</text>'.format(top + ph / 2.0, esc(ylabel)))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {0} {1}" width="{0}" '
            'height="{1}" font-family="Helvetica,Arial,sans-serif">{2}</svg>'.format(
                width, height, "".join(out)))


# ======================================================================================
# results
# ======================================================================================
class SuiteResult(object):
    """What `run_suite` did: `cases` (list of case dicts, see `new_case`), `files` (paths of
    what was written), `info` ([(label, value)] computer and software), `out_dir`."""

    def __init__(self, out_dir="", quick=True):
        self.out_dir = out_dir
        self.quick = quick
        self.cases = []
        self.files = {}
        self.info = []
        self.executables = {}
        self.started = ""
        self.runtime_s = 0.0
        self.notes = []

    def counts(self):
        c = dict((s, 0) for s in STATUSES)
        for k in self.cases:
            c[k["status"]] = c.get(k["status"], 0) + 1
        return c

    def counts_text(self):
        c = self.counts()
        return ", ".join("{} {}".format(c[s], s) for s in STATUSES)

    @property
    def ok(self):
        c = self.counts()
        return c[FAIL] == 0 and c[ERROR] == 0 and c[PASS] > 0

    def case(self, cid):
        for k in self.cases:
            if k["id"] == cid:
                return k
        return None

    def as_dict(self):
        return {"out_dir": self.out_dir, "quick": self.quick, "started": self.started,
                "runtime_s": self.runtime_s, "counts": self.counts(), "ok": self.ok,
                "info": [list(r) for r in self.info], "executables": self.executables,
                "files": self.files, "notes": self.notes,
                "cases": [_case_json(k) for k in self.cases]}


def _jsonable(v):
    if isinstance(v, dict):
        return dict((str(k), _jsonable(x)) for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, np.ndarray):
        return [_jsonable(x) for x in v.tolist()]
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def _case_json(case):
    return _jsonable(dict((k, v) for k, v in case.items() if k != "arrays"))


def new_case(cid, name=None, model=None):
    """An empty case dict. Keys: id, name, model, description, setup [(label, value)],
    reference (text), references (keys of REFS), status, metrics {key: value}, labels
    {key: label}, criteria [{key, label, value, text, passed, kind, basis}], per_time
    (optional table: {"columns": [...], "rows": [[...]]}), notes (findings), summary (one
    line), runtime_s, arrays (plot: {title, xlabel, ylabel, series, vlines}), error."""
    nm, md = CASE_INFO.get(cid, (cid, ""))
    return {"id": cid, "name": name or nm, "model": model or md, "description": "",
            "setup": [], "reference": "", "references": [], "status": SKIP, "metrics": {},
            "labels": {}, "criteria": [], "per_time": None, "notes": [], "summary": "",
            "runtime_s": 0.0, "arrays": None, "error": "", "plot_svg": "", "plot_png": ""}


def _m(case, key, value, label):
    case["metrics"][key] = value
    case["labels"][key] = label
    return value


def _crit(case, key, op, limit, kind, basis):
    """op: '<=' (value <= limit), 'abs<=' (|value| <= limit), 'between' (lo <= v <= hi)."""
    v = case["metrics"].get(key)
    ok = v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))
    if op == "<=":
        passed = ok and v <= limit
        text = "≤ {}".format(_fmt(limit))
    elif op == "abs<=":
        passed = ok and abs(v) <= limit
        text = "≤ ±{}".format(_fmt(limit))
    elif op == "between":
        passed = ok and limit[0] <= v <= limit[1]
        text = "{} to {}".format(_fmt(limit[0]), _fmt(limit[1]))
    else:
        raise ValueError(op)
    case["criteria"].append({"key": key, "label": case["labels"].get(key, key), "value": v,
                             "text": text, "passed": bool(passed), "kind": kind,
                             "basis": basis})
    return passed


def _finish(case):
    if case["status"] in (SKIP, ERROR) and not case["criteria"]:
        return case
    case["status"] = PASS if case["criteria"] and all(c["passed"] for c in case["criteria"]) \
        else FAIL
    return case


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, np.integer)):
        return "{:,}".format(int(v))
    if isinstance(v, (float, np.floating)):
        f = float(v)
        if not math.isfinite(f):
            return "—"
        a = abs(f)
        if a == 0:
            return "0"
        if a >= 1e4:
            return "{:,.0f}".format(f)
        if a >= 100:
            return "{:.1f}".format(f)
        if a >= 1:
            return "{:.3f}".format(f).rstrip("0").rstrip(".") if a < 10 else "{:.2f}".format(f)
        if a >= 1e-3:
            return "{:.4f}".format(f)
        return "{:.2e}".format(f)
    return str(v)


# ======================================================================================
# running the models
# ======================================================================================
class _Ctx(object):
    def __init__(self, lisflood_exe, anuga_python, work, quick, threads, progress, log, cancel):
        self.lisflood_exe = lisflood_exe
        self.anuga_python = anuga_python
        self.work = work
        self.quick = quick
        self.threads = threads
        self._progress = progress
        self._log = log
        self.cancel = cancel
        self.base, self.span, self.label = 0.0, 100.0, ""
        self.versions = {}

    def log(self, msg):
        if self._log:
            self._log(msg)

    def progress(self, pct, msg=""):
        if self._progress:
            p = self.base + self.span * max(0.0, min(100.0, float(pct))) / 100.0
            self._progress(p, "{}: {}".format(self.label, msg) if msg else self.label)

    def check_cancel(self):
        if self.cancel and self.cancel():
            raise Cancelled("cancelled")


def _fresh_dir(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def _zero_hydrograph(t_end):
    from .breach import Hydrograph
    return Hydrograph(np.array([0.0, float(t_end) + 60.0]), np.array([0.0, 0.0]),
                      "zero inflow (initial-condition test)")


def _hydrograph(t, q, label):
    from .breach import Hydrograph
    return Hydrograph(np.asarray(t, dtype=float), np.asarray(q, dtype=float), label)


def _run_lisflood(ctx, name, dem, inflow_xy, hydrograph, inflow_line=None, **kw):
    """write_inputs + run exactly as the plugin does. Returns (workdir, settings)."""
    if not ctx.lisflood_exe or not os.path.isfile(ctx.lisflood_exe):
        raise ModelError("LISFLOOD-FP executable not found: {}".format(ctx.lisflood_exe))
    ctx.check_cancel()
    wd = _fresh_dir(os.path.join(ctx.work, name))
    st = lf.LisfloodSettings(ctx.lisflood_exe, threads=ctx.threads, **kw)
    par = lf.write_inputs(wd, dem, inflow_xy, hydrograph, st, inflow_line=inflow_line)
    lf.run(wd, par, st, progress=ctx.progress, cancel=ctx.cancel)
    if "lisflood" not in ctx.versions:
        ctx.versions["lisflood"] = _lisflood_version(os.path.join(wd, "lisflood.log"))
    return wd, st


def _lisflood_version(log_path):
    try:
        with open(log_path, errors="replace") as f:
            head = f.read(4000)
    except OSError:
        return ""
    m = re.search(r"LISFLOOD-FP version\s+([^\n]+)", head)
    return m.group(1).strip() if m else ""


def _mass_rows(workdir, name="run"):
    """LISFLOOD-FP's .mass file: [time, tstep, min tstep, steps, area, volume, Qin, Hds,
    Qout, Qerror, Verror, rain] per mass interval."""
    rows = []
    path = os.path.join(workdir, "results", name + ".mass")
    with open(path) as f:
        next(f)
        for line in f:
            p = line.split()
            if len(p) >= 11:
                try:
                    rows.append([float(v) for v in p[:12]] + [0.0] * (12 - len(p[:12])))
                except ValueError:
                    continue
    if not rows:
        raise ModelError("LISFLOOD-FP wrote no mass-balance lines in {}".format(path))
    return np.asarray(rows)


def _snapshots(workdir, interval_s, mass=None):
    """[(index, time_s, depth array)] of the depth snapshots. With massint equal to saveint
    both are written in the same time step, so the .mass rows give the snapshots' exact times
    (a snapshot is written at the end of the first step at or past k x saveint)."""
    out = []
    for k, (idx, path) in enumerate(lf.snapshot_files(workdir)):
        g = Grid.read_asc(path)
        a = np.where(np.isfinite(g.data) & (g.data != g.nodata) & (g.data > 0), g.data, 0.0)
        t = idx * float(interval_s)
        if mass is not None and idx < len(mass):
            t = float(mass[idx, 0])
        out.append((idx, t, a))
    return out


def _snapshot_at(snaps, t_nominal, interval_s):
    k = int(round(t_nominal / float(interval_s)))
    for idx, t, a in snaps:
        if idx == k:
            return t, a
    raise ModelError("LISFLOOD-FP wrote no depth snapshot for t = {:g} s".format(t_nominal))


def _run_anuga(ctx, name, dem, outer, inlet, hydrograph, st, out_grid, initial_depth=None,
               regions=None):
    """anuga_model.write_job + the worker, as anuga_model.run does, with the mesh outline,
    inlet and output grid given here. Returns (result dict of arrays, stats)."""
    from . import anuga_model as am
    if not st.python or not os.path.isfile(st.python):
        raise ModelError("ANUGA Python interpreter not found: {}".format(st.python))
    ctx.check_cancel()
    wd = _fresh_dir(os.path.abspath(os.path.join(ctx.work, name)))
    job = os.path.join(wd, "anuga_job.npz")
    result = os.path.join(wd, "anuga_result.npz")
    am.write_job(job, dem, np.asarray(outer, float), np.zeros((0, 2)), np.asarray(inlet, float),
                 hydrograph, st, out_grid, initial_depth, None, None, regions)
    proc, log_path = am._launch(st, job, result, wd, ctx.progress, ctx.cancel)
    if proc.returncode != 0 or not os.path.exists(result):
        tail = am._tail_text(log_path)
        raise ModelError("ANUGA stopped with code {}:\n{}".format(proc.returncode, tail))
    with np.load(result) as r:          # 3.4.1: closed at once (Windows file lock)
        stats = json.loads(str(r["stats"]))
        arrays = dict((k, r[k]) for k in ("depth", "velocity", "arrival_hr", "wse"))
    if "anuga" not in ctx.versions:
        ctx.versions["anuga"] = "{} ({})".format(stats.get("anuga_version", "?"),
                                                 stats.get("algorithm", ""))
    return arrays, stats


def _anuga_settings(ctx, **kw):
    from . import anuga_model as am
    return am.AnugaSettings(ctx.anuga_python, threads=ctx.threads, **kw)


def _walled_dem(bed, dx, x_left, wall="NSEW"):
    """A Grid of `bed` (rows north to south) with a one-cell ring of wall cells WALL_M above
    the highest bed on the sides named in `wall`; the channel cells keep their bed."""
    ny, nx = bed.shape
    zw = float(np.nanmax(bed)) + WALL_M
    z = np.array(bed, dtype=float)
    if "N" in wall:
        z[0, :] = zw
    if "S" in wall:
        z[-1, :] = zw
    if "W" in wall:
        z[:, 0] = zw
    if "E" in wall:
        z[:, -1] = zw
    return Grid(z, x_left, ny * dx, dx), zw


# ======================================================================================
# case 1: Ritter (LISFLOOD-FP)
# ======================================================================================
def case_ritter(ctx):
    case = new_case("ritter")
    g, h0, T, S, n = G_LISFLOOD, 1.0, 60.0, 20.0, 0.001
    dx = 2.0 if ctx.quick else 1.0
    times = (20.0, 40.0, 60.0)
    thr = 0.05 * h0
    c0 = math.sqrt(g * h0)
    nr = int(math.ceil(1.3 * c0 * T / dx)) + 10
    nd = int(math.ceil(2.3 * c0 * T / dx)) + 10
    rows = 3
    ny, nx = rows + 2, nr + nd + 2
    x = (np.arange(nx) - 1 - nr + 0.5) * dx           # cell centres; the dam is at x = 0
    dem, _zw = _walled_dem(np.zeros((ny, nx)), dx, x[0] - 0.5 * dx)
    chan = dem.data == 0.0
    init = np.where(chan & (x[None, :] < 0.0), h0, 0.0)
    case["description"] = (
        "A dam holding {h0:g} m of still water is removed instantly at t = 0; downstream the "
        "bed is dry, horizontal and (almost) frictionless. The channel is {r} cells wide with "
        "walls on all sides and long enough that no wave reaches its ends in {T:g} s. The "
        "run starts from the initial depth (LISFLOOD-FP startfile) with a zero hydrograph at "
        "a cell at the far end of the reservoir. The depth along the channel centre is "
        "compared with Ritter's solution at {ts} s.").format(
            h0=h0, r=rows, T=T, ts=", ".join("{:g}".format(t) for t in times))
    case["setup"] = [("Upstream depth h0", "{:g} m".format(h0)),
                     ("Cell size", "{:g} m ({:,} x {} cells incl. walls)".format(dx, nx, ny)),
                     ("Reservoir / downstream length", "{:.0f} m / {:.0f} m".format(
                         nr * dx, nd * dx)),
                     ("Manning's n", "{:g} (LISFLOOD-FP's friction term needs n > 0)".format(n)),
                     ("Simulated time", "{:g} s, depth snapshots every {:g} s".format(T, S)),
                     ("Solver", "LISFLOOD-FP acceleration (local inertial), default settings "
                                "as the plugin runs it"),
                     ("g", "{} m/s² (LISFLOOD-FP's value)".format(g))]
    case["reference"] = (
        "Ritter (1892), as compiled in SWASHES (Delestre et al. 2013): h = h0 for x ≤ "
        "-c0 t; h = (2 c0 - x/t)² / (9 g) for -c0 t ≤ x ≤ 2 c0 t; h = 0 beyond, "
        "with c0 = √(g h0). The depth at the dam site stays at 4/9 h0 and the flow is "
        "supercritical (Fr > 1) everywhere downstream of the dam.")
    case["references"] = ["ritter", "swashes", "bates2010", "neal2012", "dealmeida2013"]
    wd, _st = _run_lisflood(ctx, "ritter", dem, dem.xy(ny // 2, 1), _zero_hydrograph(T),
                            manning_n=n, arrival_depth_m=thr, sim_time_s=T, massint_s=S,
                            initial_tstep_s=1.0, snapshot_s=S, initial_depth=dem.like(init))
    mass = _mass_rows(wd)
    snaps = _snapshots(wd, S, mass)
    v0 = float(init.sum()) * dx * dx
    vol_change = 100.0 * (mass[:, 5] - v0) / v0
    xc = x[1:-1]
    dam = np.argmin(np.abs(xc + 0.5 * dx)), np.argmin(np.abs(xc - 0.5 * dx))
    per, series = [], []
    for k, tn in enumerate(times):
        t, a = _snapshot_at(snaps, tn, S)
        h = a[ny // 2, 1:-1]
        ha = ritter_depth(xc, t, h0, g)
        win = (xc >= -1.25 * c0 * t) & (xc <= 2.25 * c0 * t)
        rmse = float(np.sqrt(np.mean((h[win] - ha[win]) ** 2))) / h0
        xf = _front_position(xc, h, thr)
        xfa = ritter_front(t, h0, thr, g)
        hd = 0.5 * float(h[dam[0]] + h[dam[1]]) / h0
        row = int(np.argmin(np.abs(mass[:, 0] - t)))
        per.append([t, rmse, xf, xfa, xf / xfa, hd, float(vol_change[row])])
        colr = PALETTE[k % len(PALETTE)]
        xs = np.linspace(-1.4 * c0 * T, 2.4 * c0 * T, 600)
        series.append({"label": "Ritter t = {:.0f} s".format(t), "x": xs,
                       "y": ritter_depth(xs, t, h0, g), "color": colr})
        series.append({"label": "LISFLOOD-FP t = {:.1f} s".format(t), "x": xc, "y": h,
                       "color": colr, "dash": True})
    t, rmse, xf, xfa, ratio, hd, _vc = per[-1]
    _m(case, "time_s", t, "Time compared (s)")
    _m(case, "rmse_over_h0", rmse, "RMSE of depth / h0 (x from -1.25 c0 t to 2.25 c0 t)")
    _m(case, "front_model_m", xf, "Front (depth 5% of h0), LISFLOOD-FP (m from the dam)")
    _m(case, "front_analytic_m", xfa, "Front (depth 5% of h0), Ritter (m from the dam)")
    _m(case, "front_ratio", ratio, "Front travel, LISFLOOD-FP / Ritter")
    _m(case, "dam_site_depth_over_h0", hd, "Depth at the dam site / h0 (Ritter: 4/9 = 0.444)")
    _m(case, "max_volume_change_pct", float(vol_change[np.argmax(np.abs(vol_change))]),
       "Largest change of the stored volume, % (closed channel: 0)")
    _m(case, "cell_size_m", dx, "Cell size (m)")
    case["per_time"] = {"columns": ["t (s)", "RMSE / h0", "front model (m)",
                                    "front Ritter (m)", "front ratio", "h(dam) / h0",
                                    "volume change %"],
                        "rows": per}
    _crit(case, "rmse_over_h0", "<=", 0.25, "bound",
          LIA_BASIS + " Ritter's flow is supercritical downstream of the dam, so the "
          "depth error is large by design of the scheme; 0.25 h0 is about 1.5 times what a "
          "correct build gives at 1-2 m cells (0.14-0.18), so it catches a broken or different "
          "build without claiming accuracy.")
    _crit(case, "front_ratio", "between", (0.35, 1.10), "bound",
          "Without advection the front runs at roughly half the analytic speed (0.45-0.57 of "
          "the analytic travel at 1-2 m cells). Below 0.35 or above 1.10 (faster than the "
          "physics allows) would point to a broken or different build.")
    _crit(case, "max_volume_change_pct", "abs<=", 3.0, "bound",
          "Nothing enters or leaves the walled channel, yet the local inertial scheme adds a "
          "little water where the thin supercritical front over-drains a cell (the negative "
          "depth is set to zero): +1.4% at 2 m cells and +2.1% at 1 m cells were measured in "
          "this frictionless dry-bed worst case, so this is expected behaviour of the scheme "
          "here, bounded at 3%. Strict conservation is graded in the volume-balance case "
          "(1%), which the plugin's own run check also uses.")
    case["notes"].append(
        "At t = {:.0f} s the LISFLOOD-FP front (5% of h0) is {:.0f} m from the dam against "
        "{:.0f} m in Ritter's solution ({:.0%} of the travel), and the depth at the dam site "
        "is {:.2f} h0 against 0.44 h0. This is the documented limitation of the local inertial "
        "approximation in supercritical flow, not an installation fault. For breach studies it "
        "means LISFLOOD-FP arrival times close to the dam, where the flow is supercritical, "
        "come out late (non-conservative for warning time); ANUGA solves the full shallow "
        "water equations there.".format(t, xf, xfa, ratio, hd))
    case["summary"] = "RMSE/h0 {:.3f}; front {:.0%} of Ritter; volume {:+.2f}%".format(
        rmse, ratio, case["metrics"]["max_volume_change_pct"])
    case["arrays"] = {"title": "Ritter dam break: depth along the channel (LISFLOOD-FP, "
                               "{:g} m cells)".format(dx),
                      "xlabel": "Distance from the dam (m)", "ylabel": "Depth (m)",
                      "series": series, "vlines": [(0.0, "dam")],
                      "xlim": (-1.4 * c0 * T, 2.4 * c0 * T)}
    return _finish(case)


# ======================================================================================
# case 2: Stoker (LISFLOOD-FP)
# ======================================================================================
STOKER_RATIOS = (0.1, 0.25, 0.5)


def _stoker_limits(fr):
    """Criteria by the Froude number of the analytic middle state."""
    if fr < 0.5:
        return {"rmse": 0.10, "bore": 10.0, "plateau": 10.0, "kind": "accuracy",
                "why": "Fr2 < 0.5: the range where de Almeida & Bates (2013) find good "
                       "agreement, so these are accuracy criteria (about 1.3-2 times what a "
                       "correct build gives: RMSE 0.07-0.08 h0, bore 5-6% slow, plateau "
                       "4-5% high)."}
    if fr < 1.0:
        return {"rmse": 0.20, "bore": 15.0, "plateau": 25.0, "kind": "bound",
                "why": "0.5 ≤ Fr2 < 1: subcritical but above the range of good "
                       "agreement; bounds of expected behaviour (a correct build gives RMSE "
                       "0.13-0.14 h0, bore 9% slow, plateau 19% high)."}
    return {"rmse": 0.25, "bore": 25.0, "plateau": 60.0, "kind": "bound",
            "why": "Fr2 ≥ 1: supercritical middle state, which the local inertial scheme "
                   "cannot represent; bounds of expected behaviour (a correct build gives "
                   "RMSE 0.17-0.19 h0, bore 15-17% slow, plateau 45-47% high)."}


def case_stoker(ctx):
    case = new_case("stoker")
    g, h0, T, S, n = G_LISFLOOD, 1.0, 60.0, 30.0, 0.001
    dx = 2.0 if ctx.quick else 1.0
    rows = 3
    case["description"] = (
        "As the Ritter case but with still water downstream (h1 = {} h0), so a bore runs into "
        "the tailwater. The Froude number of the flow between the rarefaction and the bore "
        "(Fr2) rises as the tailwater gets shallower, which grades what the local inertial "
        "scheme can be expected to reproduce. Compared at t = {:g} s (the {:g} s snapshot is "
        "plotted for the trend).").format(", ".join("{:g}".format(r) for r in STOKER_RATIOS),
                                         T, S)
    case["setup"] = [("Upstream depth h0", "{:g} m".format(h0)),
                     ("Tailwater h1 / h0", ", ".join("{:g}".format(r) for r in STOKER_RATIOS)),
                     ("Cell size", "{:g} m, channel {} cells wide, walled".format(dx, rows)),
                     ("Manning's n", "{:g}".format(n)),
                     ("Simulated time", "{:g} s".format(T)),
                     ("g", "{} m/s²".format(g))]
    case["reference"] = (
        "Stoker (1957), as compiled in SWASHES (Delestre et al. 2013). The middle state (h2, "
        "u2) satisfies the Riemann invariant across the rarefaction, u2 + 2√(g h2) = "
        "2√(g h0), and the Rankine-Hugoniot conditions across the bore moving at s into "
        "still water, s = √(g h2 (h1 + h2) / (2 h1)), u2 = s (1 - h1/h2); h2 is found by "
        "bisection. Depth: h0 for x ≤ -c0 t, the Ritter fan up to x = (u2 - c2) t, h2 up to "
        "x = s t, h1 beyond.")
    case["references"] = ["stoker", "swashes", "bates2010", "dealmeida2013", "neal2012"]
    series, rows_out, worst = [], [], []
    for k, r in enumerate(STOKER_RATIOS):
        ctx.check_cancel()
        h1 = r * h0
        sd = stoker_state(h0, h1, g)
        c0 = math.sqrt(g * h0)
        nr = int(math.ceil(1.3 * c0 * T / dx)) + 10
        nd = int(math.ceil(1.3 * sd["s"] * T / dx)) + 10
        ny, nx = rows + 2, nr + nd + 2
        x = (np.arange(nx) - 1 - nr + 0.5) * dx
        dem, _zw = _walled_dem(np.zeros((ny, nx)), dx, x[0] - 0.5 * dx)
        chan = dem.data == 0.0
        init = np.where(chan, np.where(x[None, :] < 0.0, h0, h1), 0.0)
        wd, _st = _run_lisflood(ctx, "stoker_{:g}".format(r), dem, dem.xy(ny // 2, 1),
                                _zero_hydrograph(T), manning_n=n, arrival_depth_m=0.05,
                                sim_time_s=T, massint_s=S, initial_tstep_s=1.0, snapshot_s=S,
                                initial_depth=dem.like(init))
        mass = _mass_rows(wd)
        snaps = _snapshots(wd, S, mass)
        v0 = float(init.sum()) * dx * dx
        vol_change = 100.0 * (mass[:, 5] - v0) / v0
        xc = x[1:-1]
        t, a = _snapshot_at(snaps, T, S)
        h = a[ny // 2, 1:-1]
        ha = stoker_depth(xc, t, h0, h1, g)
        win = (xc >= -1.25 * c0 * t) & (xc <= 1.25 * sd["s"] * t)
        rmse = float(np.sqrt(np.mean((h[win] - ha[win]) ** 2))) / h0
        xb = _front_position(xc, h, 0.5 * (h1 + sd["h2"]))
        bore_err = 100.0 * (xb - sd["s"] * t) / (sd["s"] * t)
        lo, hi = sd["fan_tail"] * t, sd["s"] * t
        pw = (xc >= lo + 0.2 * (hi - lo)) & (xc <= hi - 0.2 * (hi - lo))
        plateau = float(np.median(h[pw])) if pw.any() else float("nan")
        plat_err = 100.0 * (plateau / sd["h2"] - 1.0)
        vc = float(vol_change[np.argmax(np.abs(vol_change))])
        p = "r{:g}_".format(r)
        lab = "h1/h0 = {:g}: ".format(r)
        _m(case, p + "froude2", sd["froude2"], lab + "Fr2 of the analytic middle state")
        _m(case, p + "h2_analytic_m", sd["h2"], lab + "middle-state depth h2, Stoker (m)")
        _m(case, p + "plateau_model_m", plateau, lab + "middle-state depth, LISFLOOD-FP "
                                                       "(median over the plateau, m)")
        _m(case, p + "rmse_over_h0", rmse, lab + "RMSE of depth / h0")
        _m(case, p + "bore_error_pct", bore_err, lab + "bore position error (% of s t)")
        _m(case, p + "plateau_error_pct", plat_err, lab + "middle-state depth error (%)")
        _m(case, p + "volume_change_pct", vc, lab + "largest change of the stored volume (%)")
        lim = _stoker_limits(sd["froude2"])
        _crit(case, p + "rmse_over_h0", "<=", lim["rmse"], lim["kind"], LIA_BASIS + " " +
              lim["why"])
        _crit(case, p + "bore_error_pct", "abs<=", lim["bore"], lim["kind"], lim["why"])
        _crit(case, p + "plateau_error_pct", "abs<=", lim["plateau"], lim["kind"], lim["why"])
        _crit(case, p + "volume_change_pct", "abs<=", 2.0, "conservation",
              "Closed channel; as in the Ritter case (2%, the plugin's run check allows 1% of "
              "the injected volume in real runs).")
        rows_out.append([r, sd["froude2"], rmse, bore_err, sd["h2"], plateau, plat_err, vc])
        worst.append((r, rmse, bore_err, plat_err))
        colr = PALETTE[k % len(PALETTE)]
        xs = np.linspace(-1.35 * c0 * T, 1.3 * sd["s"] * T, 600)
        series.append({"label": "Stoker h1/h0 = {:g}".format(r), "x": xs,
                       "y": stoker_depth(xs, t, h0, h1, g), "color": colr})
        series.append({"label": "LISFLOOD-FP h1/h0 = {:g}".format(r), "x": xc, "y": h,
                       "color": colr, "dash": True})
    case["per_time"] = {"columns": ["h1/h0", "Fr2", "RMSE / h0", "bore error %",
                                    "h2 Stoker (m)", "h2 model (m)", "h2 error %",
                                    "volume change %"], "rows": rows_out}
    case["notes"].append(
        "Errors grow with the Froude number of the middle state, as the literature on the "
        "local inertial approximation predicts: RMSE/h0 {} for h1/h0 = {}; the bore runs "
        "slow and the middle state is too deep.".format(
            ", ".join("{:.3f}".format(w[1]) for w in worst),
            ", ".join("{:g}".format(w[0]) for w in worst)))
    case["summary"] = "; ".join("h1/h0 {:g}: RMSE/h0 {:.3f}, bore {:+.1f}%".format(
        w[0], w[1], w[2]) for w in worst)
    case["arrays"] = {"title": "Stoker dam break at t = {:g} s (LISFLOOD-FP, {:g} m cells)"
                               .format(T, dx),
                      "xlabel": "Distance from the dam (m)", "ylabel": "Depth (m)",
                      "series": series, "vlines": [(0.0, "dam")]}
    return _finish(case)


# ======================================================================================
# case 3: steady uniform flow (LISFLOOD-FP)
# ======================================================================================
def case_uniform(ctx):
    case = new_case("uniform")
    S, n, q, L, T = 0.001, 0.03, 1.0, 2000.0, 7200.0
    dx = 10.0 if ctx.quick else 5.0
    ncross = 5
    W = ncross * dx
    Q = q * W
    ny = ncross + 2
    nx = int(round(L / dx)) + 1                       # column 0 is the upstream wall
    z_up = 10.0 + S * L
    bed = np.round(z_up - S * dx * (np.arange(nx) - 1), 3)     # exact in the 3-decimal DEM
    dem, _zw = _walled_dem(np.repeat(bed[None, :], ny, 0), dx, 0.0, wall="NSW")
    x = (np.arange(nx) - 0.5) * dx                    # distance from the upstream wall
    hn = manning_normal_depth(q, n, S)
    hr = manning_normal_depth_rect(Q, W, n, S)
    fr = q / (hn * math.sqrt(G_LISFLOOD * hn))
    case["description"] = (
        "A constant discharge enters the top of a straight, walled rectangular channel of "
        "uniform slope and leaves at the bottom through the model's free (normal-depth) "
        "boundary. After spin-up the depth in the middle of the channel must be Manning's "
        "normal depth. The inflow ramps up over 10 minutes.")
    case["setup"] = [("Slope", "{:g}".format(S)), ("Manning's n", "{:g}".format(n)),
                     ("Discharge", "{:g} m³/s ({:g} m²/s per metre of width)".format(
                         Q, q)),
                     ("Channel", "{:g} m long, {:g} m wide ({} cells of {:g} m)".format(
                         L, W, ncross, dx)),
                     ("Simulated time", "{:g} h".format(T / 3600.0)),
                     ("Measured", "mean depth of the final snapshot over x = {:.0f}-{:.0f} m"
                                  .format(0.4 * L, 0.6 * L))]
    case["reference"] = (
        "Manning's equation (Manning 1891; Chow 1959). A 2D depth-averaged model has bed "
        "friction only, so its normal depth is the wide-channel one with the hydraulic radius "
        "equal to the depth: h = (q n / √S)^(3/5) = {:.4f} m. With the side-wall friction "
        "of a real {:g} m rectangular channel it would be {:.4f} m (not what the model "
        "solves). Froude number {:.2f} (subcritical, the solver's intended range).").format(
            hn, W, hr, fr)
    case["references"] = ["manning", "chow", "bates2010"]
    hy = _hydrograph([0.0, 600.0, T + 3600.0], [0.0, Q, Q], "constant inflow")
    y_top, y_bot = dem.xy(1, 1)[1], dem.xy(ny - 2, 1)[1]
    xin = dem.xy(1, 1)[0]
    wd, _st = _run_lisflood(ctx, "uniform", dem, (xin, 0.5 * (y_top + y_bot)), hy,
                            inflow_line=((xin, y_top), (xin, y_bot)), manning_n=n,
                            arrival_depth_m=0.05, sim_time_s=T, massint_s=300.0,
                            initial_tstep_s=10.0, snapshot_s=T / 4.0)
    mass = _mass_rows(wd)
    snaps = _snapshots(wd, T / 4.0)
    mid = (x >= 0.4 * L) & (x <= 0.6 * L)
    prof = [(t, a[1:-1, :].mean(axis=0)) for _i, t, a in snaps]
    t_end, h_end = prof[-1]
    hm = float(h_end[mid].mean())
    hp = float(prof[-2][1][mid].mean()) if len(prof) > 1 else float("nan")
    qin, qout = float(mass[-1, 6]), float(mass[-1, 8])
    _m(case, "normal_depth_m", hn, "Normal depth, Manning (wide channel, m)")
    _m(case, "normal_depth_rect_m", hr, "Normal depth with side-wall friction (information, m)")
    _m(case, "model_depth_m", hm, "LISFLOOD-FP depth mid-channel (m)")
    _m(case, "depth_error_pct", 100.0 * (hm / hn - 1.0), "Depth error (%)")
    _m(case, "outflow_over_inflow", qout / qin if qin else float("nan"),
       "Outflow / inflow at the end")
    _m(case, "steady_change_pct", 100.0 * abs(hm - hp) / hn,
       "Depth change over the last {:g} min (% of normal depth)".format(T / 4.0 / 60.0))
    _m(case, "froude", fr, "Froude number")
    _crit(case, "depth_error_pct", "abs<=", 2.0, "accuracy",
          "The model solves the same friction law, so at steady state only the discretisation "
          "and the 1 mm output resolution separate the two (0.01% measured at 5-20 m cells); "
          "2% (about 2 cm here) is well inside the vertical accuracy of LiDAR terrain.")
    _crit(case, "outflow_over_inflow", "between", (0.99, 1.01), "conservation",
          "At steady state what comes in goes out: within 1%, the plugin's volume limit.")
    _crit(case, "steady_change_pct", "<=", 0.5, "accuracy",
          "The comparison is only meaningful once the flow is steady: less than 0.5% change "
          "over the last snapshot interval.")
    case["summary"] = "normal depth {:.3f} m, model {:.3f} m ({:+.2f}%)".format(
        hn, hm, 100.0 * (hm / hn - 1.0))
    series = []
    for k, (t, h) in enumerate(prof[1:]):
        last = k == len(prof) - 2
        series.append({"label": "LISFLOOD-FP t = {:.1f} h".format(t / 3600.0),
                       "x": x[1:], "y": h[1:], "color": PALETTE[0] if last else "#9ecae1",
                       "width": 2.2 if last else 1.2})
    series.append({"label": "Manning normal depth", "x": [0.0, L], "y": [hn, hn],
                   "color": "#d62728", "dash": True})
    case["arrays"] = {"title": "Steady uniform flow, slope {:g}, n = {:g} (LISFLOOD-FP, {:g} m "
                               "cells)".format(S, n, dx),
                      "xlabel": "Distance down the channel (m)", "ylabel": "Depth (m)",
                      "series": series, "vlines": [(0.4 * L, "measured from"),
                                                   (0.6 * L, "to")]}
    return _finish(case)


# ======================================================================================
# case 4: volume balance on the synthetic valley (LISFLOOD-FP)
# ======================================================================================
MASS_HYDROGRAPH = ([0.0, 900.0, 3600.0], [0.0, 150.0, 0.0])     # s, m3/s: 270,000 m3


def _valley_grid(xmin, xmax, ymax, dx):
    from .selftest import model_valley
    nx = int(round((xmax - xmin) / dx))
    ny = int(round(2 * ymax / dx))
    xs = xmin + (np.arange(nx) + 0.5) * dx
    ys = ymax - (np.arange(ny) + 0.5) * dx
    X, Y = np.meshgrid(xs, ys)
    return Grid(model_valley(X, Y), xmin, ymax, dx)


def case_mass(ctx):
    case = new_case("mass")
    dx = 20.0 if ctx.quick else 10.0
    T = 7200.0 if ctx.quick else 10800.0
    n = 0.05
    dem = _valley_grid(-300.0, 3700.0, 1000.0, dx)
    hy = _hydrograph(MASS_HYDROGRAPH[0], MASS_HYDROGRAPH[1], "breach-like")
    vin = hy.volume_until(T)
    case["description"] = (
        "A breach-like hydrograph (rising to {:g} m³/s in {:g} min, back to zero at "
        "{:g} min) enters just below the embankment of the synthetic valley the plugin's own "
        "model tests use (a reservoir held by an embankment, a prairie valley falling 1 m/km "
        "with a small channel and gentle relief). The flood leaves through the free east edge. "
        "Three volume balances are checked: LISFLOOD-FP's own accounting, its domain volume "
        "plus the outflow, and the depth grid it wrote plus the outflow, each against the "
        "injected volume.").format(MASS_HYDROGRAPH[1][1], MASS_HYDROGRAPH[0][1] / 60.0,
                                   MASS_HYDROGRAPH[0][2] / 60.0)
    case["setup"] = [("Domain", "{:g} x {:g} m, {:g} m cells ({:,} cells)".format(
                         4000.0, 2000.0, dx, dem.nx * dem.ny)),
                     ("Manning's n", "{:g}".format(n)),
                     ("Injected volume", "{:,.0f} m³ ({:,.1f} ac-ft)".format(
                         vin, vin / 1233.48183754752)),
                     ("Simulated time", "{:g} h, mass balance every 60 s".format(T / 3600.0)),
                     ("Boundaries", "free on all edges, as in the plugin's runs")]
    case["reference"] = (
        "Conservation of volume: injected = in the domain at the end + what left through the "
        "boundaries. The outflow is integrated (trapezoid) from LISFLOOD-FP's 60 s outflow "
        "record; the model's own accounting sums its per-interval volume errors.")
    case["references"] = ["bates2010"]
    wd, _st = _run_lisflood(ctx, "mass", dem, (60.0, 0.0), hy, inflow_line=((60.0, -20.0),
                                                                           (60.0, 20.0)),
                            manning_n=n, arrival_depth_m=0.3048, sim_time_s=T, massint_s=60.0,
                            initial_tstep_s=10.0, snapshot_s=T)
    mass = _mass_rows(wd)
    snaps = _snapshots(wd, T)
    h_end = snaps[-1][2]
    t = np.concatenate([[0.0], mass[:, 0]])
    qin = np.concatenate([[0.0], mass[:, 6]])
    qout = np.concatenate([[0.0], mass[:, 8]])
    vol = np.concatenate([[0.0], mass[:, 5]])
    cum_out = np.concatenate([[0.0], np.cumsum(0.5 * (qout[1:] + qout[:-1]) * np.diff(t))])
    vout = float(cum_out[-1])
    v_end = float(mass[-1, 5])
    v_grid = float(h_end.sum()) * dx * dx
    _m(case, "injected_m3", vin, "Injected volume (hydrograph, m³)")
    _m(case, "inflow_recorded_pct", 100.0 * (_trapz(qin, t) - vin) / vin,
       "Inflow LISFLOOD-FP recorded minus the hydrograph (% of injected)")
    _m(case, "domain_volume_m3", v_end, "Volume in the domain at the end, LISFLOOD-FP (m³)")
    _m(case, "grid_volume_m3", v_grid, "Volume of the final depth grid (m³)")
    _m(case, "outflow_m3", vout, "Outflow through the edges (m³)")
    _m(case, "outflow_share_pct", 100.0 * vout / vin, "Share of the volume that left (%)")
    _m(case, "model_accounting_error_pct", 100.0 * float(mass[:, 10].sum()) / vin,
       "LISFLOOD-FP's own cumulative volume error (% of injected)")
    _m(case, "balance_error_pct", 100.0 * (v_end + vout - vin) / vin,
       "Domain volume + outflow - injected (% of injected)")
    _m(case, "grid_balance_error_pct", 100.0 * (v_grid + vout - vin) / vin,
       "Depth grid + outflow - injected (% of injected)")
    basis = ("1% of the injected volume: the limit the plugin applies to every LISFLOOD-FP "
             "run (lisflood.read_results).")
    _crit(case, "inflow_recorded_pct", "abs<=", 1.0, "conservation",
          "The point sources must inject the hydrograph (write_inputs divides it over the "
          "inflow cells and the cell width). " + basis)
    _crit(case, "model_accounting_error_pct", "abs<=", 1.0, "conservation", basis)
    _crit(case, "balance_error_pct", "abs<=", 1.0, "conservation", basis)
    _crit(case, "grid_balance_error_pct", "abs<=", 1.0, "conservation",
          basis + " The grid is written to 1 mm, which adds a little rounding.")
    case["summary"] = "balance {:+.3f}% (grid {:+.3f}%), {:.0f}% left the domain".format(
        case["metrics"]["balance_error_pct"], case["metrics"]["grid_balance_error_pct"],
        case["metrics"]["outflow_share_pct"])
    th = t / 3600.0
    t_h = np.linspace(0.0, T, 400)
    cum_in = np.array([hy.volume_until(v) for v in t_h]) / 1000.0
    case["arrays"] = {"title": "Volume balance of the breach flood (LISFLOOD-FP, {:g} m cells)"
                               .format(dx),
                      "xlabel": "Hours", "ylabel": "Volume (1000 m³)",
                      "series": [{"label": "Injected (hydrograph)", "x": t_h / 3600.0,
                                  "y": cum_in, "color": "#222222", "width": 2.4},
                                 {"label": "In the domain + outflow", "x": th,
                                  "y": (vol + cum_out) / 1000.0, "color": PALETTE[1],
                                  "dash": True, "width": 2.0},
                                 {"label": "In the domain", "x": th, "y": vol / 1000.0,
                                  "color": PALETTE[0]},
                                 {"label": "Left through the edges", "x": th,
                                  "y": cum_out / 1000.0, "color": PALETTE[2]}]}
    return _finish(case)


# ======================================================================================
# case 5: radial spreading (LISFLOOD-FP)
# ======================================================================================
def _ray_radius(h, dx, r0, c0, ang, thr):
    """Distance from the centre of cell (r0, c0) along direction `ang` (radians from east,
    anticlockwise) to where the bilinearly interpolated depth drops below thr."""
    ny, nx = h.shape
    rmax = (min(ny, nx) // 2 - 1) * dx
    r = np.arange(0.0, rmax, dx / 20.0)
    fc = c0 + r * math.cos(ang) / dx
    fr = r0 - r * math.sin(ang) / dx
    i0 = np.clip(np.floor(fr).astype(int), 0, ny - 1)
    j0 = np.clip(np.floor(fc).astype(int), 0, nx - 1)
    i1 = np.clip(i0 + 1, 0, ny - 1)
    j1 = np.clip(j0 + 1, 0, nx - 1)
    tr, tc = fr - np.floor(fr), fc - np.floor(fc)
    v = (h[i0, j0] * (1 - tr) * (1 - tc) + h[i0, j1] * (1 - tr) * tc
         + h[i1, j0] * tr * (1 - tc) + h[i1, j1] * tr * tc)
    below = np.nonzero(v < thr)[0]
    if not len(below):
        return float("nan")
    k = int(below[0])
    if k == 0:
        return 0.0
    return float(r[k - 1] + (v[k - 1] - thr) / (v[k - 1] - v[k]) * (r[k] - r[k - 1]))


def case_radial(ctx):
    case = new_case("radial")
    dx = 10.0 if ctx.quick else 5.0
    N = 81 if ctx.quick else 161
    Q, T, n, thr = 20.0, 1800.0, 0.05, 0.05
    c = N // 2
    dem = Grid(np.zeros((N, N)), 0.0, N * dx, dx)
    case["description"] = (
        "A constant inflow at one cell spreads over a flat, uniformly rough plane. The flood "
        "outline should be a circle; a raster scheme that exchanges water only with its four "
        "neighbours can spread faster along the grid axes than along the diagonals. The flood "
        "radius (depth {:g} m) is measured along the four axes and the four diagonals from "
        "the bilinearly interpolated depth.").format(thr)
    case["setup"] = [("Plane", "{} x {} cells of {:g} m, flat, free edges".format(N, N, dx)),
                     ("Inflow", "{:g} m³/s at the centre cell for {:g} min".format(
                         Q, T / 60.0)),
                     ("Manning's n", "{:g}".format(n)),
                     ("Flood edge", "depth {:g} m".format(thr))]
    case["reference"] = (
        "Symmetry: on a flat isotropic plane the solution is radially symmetric, so the "
        "radius must not depend on direction. The four axis radii must match each other, as "
        "must the four diagonal radii (any difference is a code or threading fault); the "
        "diagonal/axis ratio documents the grid-orientation effect.")
    case["references"] = ["bates2010"]
    hy = _hydrograph([0.0, T + 3600.0], [Q, Q], "constant inflow")
    wd, _st = _run_lisflood(ctx, "radial", dem, dem.xy(c, c), hy, manning_n=n,
                            arrival_depth_m=thr, sim_time_s=T, massint_s=300.0,
                            initial_tstep_s=10.0, snapshot_s=T)
    h = _snapshots(wd, T)[-1][2]
    axes = [_ray_radius(h, dx, c, c, a, thr) for a in (0.0, 0.5 * math.pi, math.pi,
                                                       1.5 * math.pi)]
    diag = [_ray_radius(h, dx, c, c, a, thr) for a in (0.25 * math.pi, 0.75 * math.pi,
                                                       1.25 * math.pi, 1.75 * math.pi)]
    ra, rd = float(np.mean(axes)), float(np.mean(diag))
    area = float((h >= thr).sum()) * dx * dx
    edge = float(max(h[0].max(), h[-1].max(), h[:, 0].max(), h[:, -1].max()))
    ax10 = np.mean([_ray_radius(h, dx, c, c, a, 0.1) for a in (0.0, 0.5 * math.pi)])
    dg10 = np.mean([_ray_radius(h, dx, c, c, a, 0.1) for a in (0.25 * math.pi, 0.75 * math.pi)])
    for name, v in zip(("east", "north", "west", "south"), axes):
        _m(case, "radius_" + name + "_m", v, "Radius to the {} (m)".format(name))
    for name, v in zip(("ne", "nw", "sw", "se"), diag):
        _m(case, "radius_" + name + "_m", v, "Radius to the {} (m)".format(name.upper()))
    _m(case, "diagonal_over_axis", rd / ra, "Diagonal / axis radius (depth {:g} m)".format(thr))
    _m(case, "diagonal_over_axis_10cm", float(dg10 / ax10),
       "Diagonal / axis radius (depth 0.1 m, information)")
    _m(case, "axis_spread_pct", 100.0 * (max(axes) - min(axes)) / ra,
       "Spread of the four axis radii (% of their mean)")
    _m(case, "diagonal_spread_pct", 100.0 * (max(diag) - min(diag)) / rd,
       "Spread of the four diagonal radii (% of their mean)")
    _m(case, "equivalent_over_axis", math.sqrt(area / math.pi) / ra,
       "Radius of a circle of the flooded area / axis radius")
    _m(case, "edge_depth_m", edge, "Largest depth on the domain edge (m)")
    _m(case, "cells_across_radius", ra / dx, "Cells across the radius")
    _crit(case, "axis_spread_pct", "<=", 1.0, "accuracy",
          "Symmetry: identical physics in the four axis directions. Any real spread points "
          "to a code or threading fault; 1% allows the 1 mm output rounding.")
    _crit(case, "diagonal_spread_pct", "<=", 1.0, "accuracy", "As for the axes.")
    _crit(case, "diagonal_over_axis", "between", (0.95, 1.05), "accuracy",
          "Grid orientation: the outline is a circle within 5% (a correct build gives "
          "0.98-0.99 here, about one cell short on the diagonal at 30 cells of radius). 5% is "
          "small next to the effect of roughness on flood extents.")
    _crit(case, "edge_depth_m", "<=", thr, "accuracy",
          "The flood must stay inside the plane for the radii to mean anything.")
    angs = np.arange(0.0, 360.0 + 1e-9, 5.0)
    rad = np.array([_ray_radius(h, dx, c, c, math.radians(a), thr) for a in angs])
    case["summary"] = "diagonal/axis {:.3f}; axis spread {:.2f}%".format(
        rd / ra, case["metrics"]["axis_spread_pct"])
    case["notes"].append(
        "The flood radius is {:.0f} m along the axes and {:.0f} m along the diagonals "
        "({:.1%}): LISFLOOD-FP's four-neighbour scheme spreads very slightly faster along the "
        "grid axes; the effect is {:.1f} cells at this radius.".format(
            ra, rd, rd / ra, (ra - rd) / dx))
    case["arrays"] = {"title": "Flood radius by direction, depth {:g} m (LISFLOOD-FP, {:g} m "
                               "cells)".format(thr, dx),
                      "xlabel": "Direction (degrees anticlockwise from east)",
                      "ylabel": "Radius (m)",
                      "series": [{"label": "LISFLOOD-FP", "x": angs, "y": rad,
                                  "color": PALETTE[0], "marker": True},
                                 {"label": "Mean of the four axes", "x": [0.0, 360.0],
                                  "y": [ra, ra], "color": "#d62728", "dash": True}],
                      "vlines": [(a, "") for a in (45.0, 135.0, 225.0, 315.0)],
                      "ylim": (0.8 * min(rad.min(), ra), 1.1 * max(rad.max(), ra)),
                      "xlim": (0.0, 360.0)}
    return _finish(case)


# ======================================================================================
# case 6: ANUGA
# ======================================================================================
def case_anuga_ritter(ctx):
    case = new_case("anuga_ritter")
    g, h0, T = G_ANUGA, 1.0, 40.0
    area = 2.0 if ctx.quick else 1.0
    W, wall, ddx, odx = 10.0, 1.0, 0.2, 0.25
    thr = 0.05 * h0
    c0 = math.sqrt(g * h0)
    Lr = round(1.4 * c0 * T + 10.0)
    Ld = round(2.3 * c0 * T + 10.0)
    nx, ny = int(round((Lr + Ld) / ddx)), int(round((W + 2 * wall) / ddx))
    dem = Grid(np.zeros((ny, nx)), -Lr, W + wall, ddx)
    X, Y = np.meshgrid(dem.x_centers(), dem.y_centers())
    inside = (Y > 0.0) & (Y < W)
    dem.data = np.where(inside, 0.0, 5.0)
    init = dem.like(np.where(inside & (X < 0.0), h0, 0.0))
    outer = [[-Lr, -wall], [Ld, -wall], [Ld, W + wall], [-Lr, W + wall]]
    regions = [{"poly": [[-Lr + 1.0, 0.0], [Ld - 1.0, 0.0], [Ld - 1.0, W], [-Lr + 1.0, W]],
                "area": area}]
    inlet = [[-Lr + 3.0, 1.0], [-Lr + 3.0, W - 1.0]]
    nout = int(round((Lr + Ld) / odx))
    out = Grid(np.zeros((1, nout)), -Lr, 0.5 * W + 0.5 * odx, odx)
    case["description"] = (
        "Ritter's dam break (as case 1) in ANUGA, run through the plugin's ANUGA worker. The "
        "worker has no initial-condition-only mode, so it gets a zero hydrograph at an inlet "
        "in the still reservoir far upstream (ANUGA's inlet operator then only levels the "
        "already level water there). The worker returns the maximum-depth envelope; "
        "downstream of the dam Ritter's depth only rises with time, so there the envelope is "
        "the depth at t = {:g} s. The channel is walled by high ground inside the mesh (the "
        "worker's transmissive outline would exchange water through the channel's sides) and "
        "a refined-region polygon along the walls makes them mesh edges.").format(T)
    case["setup"] = [("Upstream depth h0", "{:g} m".format(h0)),
                     ("Channel", "{:g} m wide, {:.0f} m of reservoir and {:.0f} m downstream"
                                 .format(W, Lr, Ld)),
                     ("Mesh", "triangles up to {:g} m² in the channel".format(area)),
                     ("Friction", "none (n = 0)"),
                     ("Simulated time", "{:g} s".format(T)),
                     ("g", "{} m/s² (ANUGA's value)".format(g)),
                     ("Compared", "maximum depth along the channel centre, x ≥ 5 m")]
    case["reference"] = case_reference_ritter_short()
    case["references"] = ["ritter", "swashes", "anuga"]
    st = _anuga_settings(ctx, manning_n=0.0, arrival_depth_m=thr, sim_time_s=T,
                         yieldstep_s=T / 4.0, fine_area_m2=area, coarse_area_m2=area,
                         velocity_min_depth_m=0.001)
    res, stats = _run_anuga(ctx, "anuga_ritter", dem, outer, inlet, _zero_hydrograph(T), st,
                            out, initial_depth=init, regions=regions)
    xo = out.x_centers()
    h = np.nan_to_num(np.asarray(res["depth"], float)[0])
    ha = ritter_depth(xo, T, h0, g)
    win = (xo >= 5.0) & (xo <= 2.25 * c0 * T)
    rmse = float(np.sqrt(np.mean((h[win] - ha[win]) ** 2))) / h0
    xf = _front_position(xo, h, thr)
    xfa = ritter_front(T, h0, thr, g)
    v0, v1 = float(stats["initial_volume_m3"]), float(stats["volume_in_domain_m3"])
    _m(case, "rmse_over_h0", rmse, "RMSE of depth / h0 (x = 5 m to 2.25 c0 t)")
    _m(case, "front_model_m", xf, "Front (depth 5% of h0), ANUGA (m)")
    _m(case, "front_analytic_m", xfa, "Front (depth 5% of h0), Ritter (m)")
    _m(case, "front_ratio", xf / xfa, "Front travel, ANUGA / Ritter")
    _m(case, "volume_change_pct", 100.0 * (v1 - v0) / v0 if v0 else float("nan"),
       "Change of the volume in the mesh (%)")
    _m(case, "triangles", int(stats.get("triangles", 0)), "Triangles")
    _crit(case, "rmse_over_h0", "<=", 0.02, "accuracy",
          "ANUGA solves the full shallow water equations with a shock-capturing finite-volume "
          "scheme, which reproduces Ritter's solution closely (0.1-0.2% of h0 measured at "
          "1-2 m² triangles); 2% of h0 still separates it clearly from the local inertial "
          "scheme (about 16%).")
    _crit(case, "front_ratio", "between", (0.95, 1.05), "accuracy",
          "Front within 5% of the analytic travel (within 0.2% measured).")
    _crit(case, "volume_change_pct", "abs<=", 0.1, "conservation",
          "The finite-volume scheme conserves volume to round-off and no water reaches the "
          "mesh outline in this test; 0.1% would reveal a leak.")
    case["summary"] = "RMSE/h0 {:.4f}; front {:.1%} of Ritter; volume {:+.1e}%".format(
        rmse, xf / xfa, case["metrics"]["volume_change_pct"])
    xs = np.linspace(-1.2 * c0 * T, 2.3 * c0 * T, 600)
    keep = xo >= 5.0
    case["arrays"] = {"title": "Ritter dam break at t = {:g} s (ANUGA, triangles up to {:g} "
                               "m²)".format(T, area),
                      "xlabel": "Distance from the dam (m)", "ylabel": "Depth (m)",
                      "series": [{"label": "Ritter", "x": xs, "y": ritter_depth(xs, T, h0, g),
                                  "color": "#222222"},
                                 {"label": "ANUGA (maximum depth, x ≥ 5 m)", "x": xo[keep],
                                  "y": h[keep], "color": PALETTE[1], "dash": True}],
                      "vlines": [(0.0, "dam")], "xlim": (-1.2 * c0 * T, 2.3 * c0 * T)}
    return _finish(case)


def case_reference_ritter_short():
    return ("Ritter (1892), as compiled in SWASHES (Delestre et al. 2013): h = (2 c0 - x/t)² "
            "/ (9 g) between -c0 t and 2 c0 t, c0 = √(g h0).")


def case_anuga_uniform(ctx):
    case = new_case("anuga_uniform")
    S, n, q, L, W, T = 0.02, 0.03, 1.0, 600.0, 20.0, 900.0
    area = 10.0 if ctx.quick else 4.0
    wall, ddx = 4.0, 1.0
    Q = q * W
    hn = manning_normal_depth(q, n, S)
    fr = q / (hn * math.sqrt(G_ANUGA * hn))
    nx, ny = int(round((L + wall) / ddx)), int(round((W + 2 * wall) / ddx))
    dem = Grid(np.zeros((ny, nx)), -wall, W + wall, ddx)
    X, Y = np.meshgrid(dem.x_centers(), dem.y_centers())
    bed = 10.0 + S * (L - X)
    inside = (X > 0.0) & (Y > 0.0) & (Y < W)
    dem.data = np.where(inside, bed, 10.0 + S * L + 5.0)
    outer = [[-wall, -wall], [L, -wall], [L, W + wall], [-wall, W + wall]]
    regions = [{"poly": [[0.0, 0.0], [L - 2.0, 0.0], [L - 2.0, W], [0.0, W]], "area": area}]
    inlet = [[5.0, 1.0], [5.0, W - 1.0]]
    out = Grid(np.zeros((1, int(L))), 0.0, 0.5 * W + 0.5, 1.0)
    case["description"] = (
        "Steady uniform flow in ANUGA through the plugin's worker. The worker puts a "
        "transmissive boundary on the whole mesh outline, which at a channel outlet acts as a "
        "free overfall: on a mild slope the drawdown towards it (an M2 profile) reaches far "
        "up the channel and would spoil the comparison. On a steep slope the flow is "
        "supercritical, the outlet has no influence upstream and the transmissive boundary "
        "is exact, so this case uses slope {:g} (Fr = {:.2f}; below 1.5, where roll waves "
        "could form). The channel is walled by high ground inside the mesh; the inflow ramps "
        "up over 5 minutes, so the maximum-depth envelope the worker returns is the steady "
        "depth.").format(S, fr)
    case["setup"] = [("Slope", "{:g}".format(S)), ("Manning's n", "{:g}".format(n)),
                     ("Discharge", "{:g} m³/s ({:g} m²/s per metre)".format(Q, q)),
                     ("Channel", "{:g} m long, {:g} m wide".format(L, W)),
                     ("Mesh", "triangles up to {:g} m² in the channel".format(area)),
                     ("Simulated time", "{:g} min".format(T / 60.0)),
                     ("Measured", "median of the maximum depth along the centre, "
                                  "x = {:.0f}-{:.0f} m (mean and largest also reported)"
                                  .format(0.3 * L, 0.7 * L))]
    case["reference"] = (
        "Manning's equation (Manning 1891; Chow 1959) with the hydraulic radius equal to the "
        "depth (a 2D model has bed friction only): h = (q n / √S)^(3/5) = {:.4f} m.".format(
            hn))
    case["references"] = ["manning", "chow", "anuga"]
    hy = _hydrograph([0.0, 300.0, T + 3600.0], [0.0, Q, Q], "constant inflow")
    st = _anuga_settings(ctx, manning_n=n, arrival_depth_m=0.05, sim_time_s=T,
                         yieldstep_s=T / 6.0, fine_area_m2=area, coarse_area_m2=4.0 * area)
    res, stats = _run_anuga(ctx, "anuga_uniform", dem, outer, inlet, hy, st, out,
                            regions=regions)
    xo = out.x_centers()
    h = np.asarray(res["depth"], float)[0]
    mid = (xo >= 0.3 * L) & (xo <= 0.7 * L)
    hm = float(np.nanmedian(h[mid]))
    hmean, hmax = float(np.nanmean(h[mid])), float(np.nanmax(h[mid]))
    _m(case, "normal_depth_m", hn, "Normal depth, Manning (m)")
    _m(case, "model_depth_m", hm, "ANUGA depth mid-channel, median of the envelope (m)")
    _m(case, "depth_error_pct", 100.0 * (hm / hn - 1.0), "Depth error of the median (%)")
    _m(case, "model_depth_mean_m", hmean, "ANUGA depth mid-channel, mean of the envelope (m)")
    _m(case, "model_depth_max_m", hmax, "ANUGA depth mid-channel, largest of the envelope (m)")
    _m(case, "froude", fr, "Froude number")
    _m(case, "triangles", int(stats.get("triangles", 0)), "Triangles")
    _crit(case, "depth_error_pct", "abs<=", 3.0, "accuracy",
          "As for LISFLOOD-FP (case 3): the model solves the same friction law, so at steady "
          "state only the discretisation separates the two (0.4% on the quick mesh, 1.4% on the "
          "full one). The worker returns the maximum depth over time, which keeps the crests of "
          "the waves that the inflow sets up in this supercritical flow; the median of it "
          "discards them. 3% (0.012 m) is far inside the vertical accuracy of LiDAR terrain.")
    case["notes"].append(
        "The worker returns only the maximum depth over time. Crests of waves from the inflow "
        "raise its mean along the channel to {:.3f} m ({:+.1f}%) and its largest value to "
        "{:.3f} m; the finer the mesh the sharper the crests. The median, {:.3f} m, is the "
        "steady depth the case grades.".format(hmean, 100.0 * (hmean / hn - 1.0), hmax, hm))
    case["summary"] = "normal depth {:.3f} m, model {:.3f} m ({:+.2f}%)".format(
        hn, hm, 100.0 * (hm / hn - 1.0))
    case["arrays"] = {"title": "Steady uniform flow, slope {:g}, n = {:g} (ANUGA)".format(S, n),
                      "xlabel": "Distance down the channel (m)", "ylabel": "Depth (m)",
                      "series": [{"label": "ANUGA maximum depth", "x": xo[20:], "y": h[20:],
                                  "color": PALETTE[0]},
                                 {"label": "Manning normal depth", "x": [0.0, L], "y": [hn, hn],
                                  "color": "#d62728", "dash": True}],
                      "vlines": [(0.3 * L, "measured from"), (0.7 * L, "to")],
                      "ylim": (0.0, 1.6 * hn)}
    return _finish(case)


def case_anuga_mass(ctx):
    case = new_case("anuga_mass")
    area = 1500.0 if ctx.quick else 400.0
    T, n, dx = 3600.0, 0.05, 20.0
    xmin, xmax, ymax = -200.0, 3000.0, 800.0
    dem = _valley_grid(xmin, xmax, ymax, dx)
    hy = _hydrograph(MASS_HYDROGRAPH[0], MASS_HYDROGRAPH[1], "breach-like")
    vin = hy.volume_until(T)
    outer = [[xmin, -ymax], [xmax, -ymax], [xmax, ymax], [xmin, ymax]]
    inlet = [[60.0, -20.0], [60.0, 20.0]]
    case["description"] = (
        "The breach-like hydrograph of case 4 on the same synthetic valley in ANUGA, through "
        "the plugin's worker. The mesh is large enough that the flood does not reach its "
        "outline in {:g} h, so nothing leaves and the volume in the mesh at the end must equal "
        "the injected volume (the worker reports both).").format(T / 3600.0)
    case["setup"] = [("Mesh", "{:g} x {:g} m, triangles up to {:g} m²".format(
                         xmax - xmin, 2 * ymax, area)),
                     ("Manning's n", "{:g}".format(n)),
                     ("Injected volume", "{:,.0f} m³".format(vin)),
                     ("Simulated time", "{:g} h".format(T / 3600.0))]
    case["reference"] = "Conservation of volume: injected = in the mesh at the end."
    case["references"] = ["anuga"]
    st = _anuga_settings(ctx, manning_n=n, arrival_depth_m=0.3048, sim_time_s=T,
                         yieldstep_s=T / 6.0, fine_area_m2=area, coarse_area_m2=area)
    res, stats = _run_anuga(ctx, "anuga_mass", dem, outer, inlet, hy, st, dem)
    v0, v1 = float(stats["initial_volume_m3"]), float(stats["volume_in_domain_m3"])
    edge = float(stats.get("max_edge_depth_m", float("nan")))
    _m(case, "injected_m3", vin, "Injected volume (hydrograph, m³)")
    _m(case, "injected_by_worker_m3", float(stats.get("injected_m3", float("nan"))),
       "Injected volume the worker reports (m³)")
    _m(case, "final_volume_m3", v1 - v0, "Volume in the mesh at the end, less the initial (m³)")
    _m(case, "balance_error_pct", 100.0 * (v1 - v0 - vin) / vin,
       "Volume in the mesh - injected (% of injected)")
    _m(case, "edge_depth_m", edge, "Largest depth next to the mesh outline (m)")
    _m(case, "triangles", int(stats.get("triangles", 0)), "Triangles")
    _crit(case, "balance_error_pct", "abs<=", 0.1, "conservation",
          "ANUGA's finite-volume scheme and its inlet operator conserve volume to round-off "
          "(about 1e-7 measured); 0.1% would reveal water lost or created.")
    _crit(case, "edge_depth_m", "<=", 0.01, "conservation",
          "No water may reach the outline, or the balance would miss what left.")
    case["summary"] = "balance {:+.1e}%; flood kept inside the mesh".format(
        case["metrics"]["balance_error_pct"])
    d = np.asarray(res["depth"], float)
    row = int(np.argmin(np.abs(dem.y_centers())))
    xs = dem.x_centers()
    case["arrays"] = {"title": "Maximum depth along the valley centre line (ANUGA)",
                      "xlabel": "Distance from the dam (m)", "ylabel": "Maximum depth (m)",
                      "series": [{"label": "ANUGA", "x": xs, "y": np.nan_to_num(d[row]),
                                  "color": PALETTE[0]}],
                      "vlines": [(xmax, "mesh edge")], "xlim": (xmin, xmax + 50.0)}
    return _finish(case)


CASE_FUNCS = {"ritter": case_ritter, "stoker": case_stoker, "uniform": case_uniform,
              "mass": case_mass, "radial": case_radial, "anuga_ritter": case_anuga_ritter,
              "anuga_uniform": case_anuga_uniform, "anuga_mass": case_anuga_mass}


# ======================================================================================
# the suite
# ======================================================================================
def select_cases(cases=None):
    """Case ids from None/'all', a list, or a comma-separated text; 'lisflood' and 'anuga'
    stand for their groups. Raises ValueError for an unknown id."""
    if cases is None:
        return list(CASE_IDS)
    if isinstance(cases, str):
        cases = [c for c in re.split(r"[,;\s]+", cases) if c]
    out = []
    for c in cases:
        c = str(c).strip().lower()
        group = {"all": CASE_IDS, "lisflood": LISFLOOD_CASES, "anuga": ANUGA_CASES}.get(c)
        for k in (group or (c,)):
            if k not in CASE_IDS:
                raise ValueError("unknown verification case '{}' (known: {})".format(
                    k, ", ".join(CASE_IDS)))
            if k not in out:
                out.append(k)
    return out or list(CASE_IDS)


def file_fingerprint(path):
    """{'path', 'exists', 'size', 'modified', 'sha256'} of a file."""
    info = {"path": path or "", "exists": False, "size": None, "modified": "", "sha256": ""}
    if path and os.path.isfile(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        stt = os.stat(path)
        info.update(exists=True, size=int(stt.st_size), sha256=h.hexdigest(),
                    modified=datetime.datetime.fromtimestamp(stt.st_mtime).replace(
                        microsecond=0).isoformat(sep=" "))
    return info


def _plugin_version():
    try:
        from .selftest import plugin_version
        return plugin_version()
    except Exception:
        return "?"


def machine_info(lisflood_exe="", anuga_python=""):
    """([(label, value)], {"lisflood": fingerprint, "anuga_python": ..., "anuga_worker": ...})
    for the record header. Never raises."""
    info = []
    now = datetime.datetime.now().astimezone().replace(microsecond=0)
    info.append(("Date and time", now.isoformat(sep=" ")))
    info.append(("Computer", platform.node() or "?"))
    info.append(("Operating system", "{} {} ({})".format(platform.system(), platform.release(),
                                                         platform.machine())))
    info.append(("Platform", platform.platform()))
    info.append(("Processors", str(os.cpu_count() or "?")))
    info.append(("Python", "{} at {}".format(sys.version.split()[0], sys.executable)))
    info.append(("numpy", np.__version__))
    info.append(("Plugin version", _plugin_version()))
    exes = {}
    try:
        from . import anuga_model as am
        worker = am.WORKER
    except Exception:
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anuga_worker.py")
    for key, label, path in (("lisflood", "LISFLOOD-FP executable", lisflood_exe),
                             ("anuga_python", "ANUGA Python interpreter", anuga_python),
                             ("anuga_worker", "ANUGA worker script (plugin)", worker)):
        if key != "anuga_worker" and not path:
            info.append((label, "not given"))
            continue
        if key == "anuga_worker" and not anuga_python:
            continue
        try:
            fp = file_fingerprint(path)
        except OSError as e:
            fp = {"path": path, "exists": False, "error": str(e)}
        exes[key] = fp
        info.append((label, path if fp.get("exists") else "{} (not found)".format(path)))
        if fp.get("exists"):
            info.append(("  SHA-256", fp["sha256"]))
            info.append(("  size, modified", "{:,} bytes, {}".format(fp["size"],
                                                                    fp["modified"])))
    return info, exes


def run_suite(lisflood_exe, anuga_python=None, out_dir=None, cases=None, quick=True,
              progress=None, log=None, cancel=None, threads=0, figures=True, svg_backends=None,
              keep_work=True):
    """Run the verification cases and write the record. Returns a SuiteResult.

    lisflood_exe: the (patched) LISFLOOD-FP executable the plugin uses. anuga_python: the
    ANUGA interpreter, or None to skip the ANUGA cases. out_dir: output folder (created; a
    new temporary folder when None). cases: ids (see `select_cases`); None = all. quick:
    coarser grids and meshes (about a minute in all). progress(pct, text), log(text),
    cancel() -> bool: callbacks; cancelling raises lisflood.Cancelled. figures: embed the
    plots in the Word record as PNG when an SVG converter exists (`svg_backends` limits which
    are tried; see report_docx.svg_to_png). keep_work: keep the model files under work/."""
    todo = select_cases(cases)
    if not out_dir:
        out_dir = tempfile.mkdtemp(prefix="ndbreach_verification_")
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    work = os.path.join(out_dir, "work")
    os.makedirs(work, exist_ok=True)
    res = SuiteResult(out_dir, quick)
    t_start = time.time()
    res.info, res.executables = machine_info(lisflood_exe, anuga_python)
    res.started = res.info[0][1]
    ctx = _Ctx(lisflood_exe, anuga_python, work, quick, threads, progress, log, cancel)
    ctx.log("Verification suite ({} mode) in {}".format("quick" if quick else "full", out_dir))
    for i, cid in enumerate(todo):
        ctx.check_cancel()
        ctx.base, ctx.span = 100.0 * i / len(todo), 100.0 / len(todo)
        name, model = CASE_INFO[cid]
        ctx.label = "{} ({})".format(name, model)
        ctx.progress(0.0, "starting")
        t0 = time.time()
        if model == "ANUGA" and not anuga_python:
            case = new_case(cid)
            case["error"] = ("No ANUGA Python interpreter was given (ANUGA is only needed for "
                             "the ANUGA tier).")
            case["summary"] = "not run: no ANUGA interpreter"
        elif model == "LISFLOOD-FP" and not lisflood_exe:
            case = new_case(cid)
            case["status"] = ERROR
            case["error"] = "No LISFLOOD-FP executable was given."
            case["summary"] = "not run: no LISFLOOD-FP executable"
        else:
            try:
                case = CASE_FUNCS[cid](ctx)
            except Cancelled:
                raise
            except Exception as e:           # a case that cannot run is reported, not raised
                case = new_case(cid)
                case["status"] = ERROR
                case["error"] = "{}: {}".format(type(e).__name__, str(e).strip())
                case["detail"] = traceback.format_exc()[-3000:]
                case["summary"] = "could not run: " + str(e).strip().splitlines()[0][:160] \
                    if str(e).strip() else "could not run"
        case["runtime_s"] = round(time.time() - t0, 2)
        res.cases.append(case)
        ctx.log("[{}] {} ({}), {:.1f} s: {}".format(case["status"], case["name"], case["model"],
                                                    case["runtime_s"], case["summary"]))
    for k, v in ctx.versions.items():
        label = {"lisflood": "LISFLOOD-FP version (from its log)",
                 "anuga": "ANUGA version (from the worker)"}.get(k, k)
        res.info.append((label, v))
    res.runtime_s = round(time.time() - t_start, 1)
    if progress:
        progress(99.0, "writing the record")
    write_outputs(res, figures=figures, svg_backends=svg_backends)
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
        res.files.pop("work", None)
    if progress:
        progress(100.0, "done")
    ctx.log("Verification: {} ({:.0f} s). Record: {}".format(res.counts_text(), res.runtime_s,
                                                           res.files.get("docx", "")))
    return res


# ======================================================================================
# writing the record
# ======================================================================================
def write_plots(res):
    """plots/<id>.svg for every case with plot arrays. Returns {id: path}."""
    d = os.path.join(res.out_dir, "plots")
    os.makedirs(d, exist_ok=True)
    out = {}
    for case in res.cases:
        spec = case.get("arrays")
        if not spec or not spec.get("series"):
            continue
        svg = plot_svg(spec["series"], spec.get("title", case["name"]), spec.get("xlabel", ""),
                       spec.get("ylabel", ""), xlim=spec.get("xlim"), ylim=spec.get("ylim"),
                       vlines=spec.get("vlines"))
        p = os.path.join(d, case["id"] + ".svg")
        with open(p, "w", encoding="utf-8") as f:
            f.write(svg)
        case["plot_svg"] = p
        out[case["id"]] = p
    return out


def _md_cell(v):
    return str(v).replace("|", "/").replace("\n", " ")


def summary_markdown(res, plot_marks=False):
    """The summary as markdown (the subset products.markdown_to_html reads)."""
    c = res.counts()
    md = ["# Verification of the hydraulic model codes", "",
          "Result: **{}** ({}). {} mode; {:.0f} s.".format(
              "all checks passed" if res.ok else "NOT all checks passed", res.counts_text(),
              "Quick" if res.quick else "Full", res.runtime_s), "",
          "Each case runs a small problem with a known answer through the installed model "
          "exactly as the plugin runs it. The tolerances are this tool's acceptance criteria "
          "(basis given per criterion), not a regulatory standard. 'Expected behaviour' "
          "criteria bound what the scheme is known to do where it cannot reproduce the "
          "solution; they detect a broken or different build, they do not claim accuracy.", "",
          "## Summary", "", "| # | Case | Model | Result | Key measures | Time (s) |",
          "|---|---|---|---|---|---|"]
    for i, k in enumerate(res.cases, 1):
        md.append("| {} | {} | {} | **{}** | {} | {:.1f} |".format(
            i, _md_cell(k["name"]), k["model"], k["status"], _md_cell(k["summary"]),
            k["runtime_s"]))
    md += ["", "## Computer and software", "", "| Item | Value |", "|---|---|"]
    md += ["| {} | {} |".format(_md_cell(a.strip()), _md_cell(b)) for a, b in res.info]
    for i, k in enumerate(res.cases, 1):
        md += ["", "## {}. {} ({}): {}".format(i, k["name"], k["model"], k["status"]), ""]
        if k.get("description"):
            md += [k["description"], ""]
        if k.get("error"):
            md += ["**{}** {}".format("Not run:" if k["status"] == SKIP else "Error:",
                                      _md_cell(k["error"])), ""]
        if k.get("reference"):
            md += ["Reference: " + k["reference"], ""]
        if k["criteria"]:
            md += ["| Criterion | Measured | Limit | Type | Result |", "|---|---|---|---|---|"]
            for cr in k["criteria"]:
                md.append("| {} | {} | {} | {} | {} |".format(
                    _md_cell(cr["label"]), _fmt(cr["value"]), cr["text"],
                    KIND_LABELS.get(cr["kind"], cr["kind"]), "pass" if cr["passed"]
                    else "**FAIL**"))
            md.append("")
        pt = k.get("per_time")
        if pt:
            md += ["| " + " | ".join(pt["columns"]) + " |",
                   "|" + "---|" * len(pt["columns"])]
            md += ["| " + " | ".join(_fmt(v) for v in r) + " |" for r in pt["rows"]]
            md.append("")
        for note in k.get("notes") or []:
            md.append("- " + note)
        if k.get("notes"):
            md.append("")
        if k.get("plot_svg"):
            md += ["@@PLOT:{}@@".format(k["id"]) if plot_marks else
                   "Plot: plots/{}.svg".format(k["id"]), ""]
    refs = []
    for k in res.cases:
        for r in k.get("references") or []:
            if r not in refs:
                refs.append(r)
    if refs:
        md += ["## References", ""] + ["- " + REFS[r] for r in refs if r in REFS]
    return "\n".join(md) + "\n"


def write_csv(res, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "case", "model", "status", "metric", "label", "value",
                    "criterion", "criterion_type", "passed"])
        for k in res.cases:
            crit = dict((c["key"], c) for c in k["criteria"])
            if not k["metrics"]:
                w.writerow([k["id"], k["name"], k["model"], k["status"], "", "", "", "", "", ""])
            for key, v in k["metrics"].items():
                c = crit.get(key)
                w.writerow([k["id"], k["name"], k["model"], k["status"], key,
                            k["labels"].get(key, key),
                            "" if v is None else (repr(float(v)) if isinstance(
                                v, (float, np.floating)) else v),
                            c["text"] if c else "", KIND_LABELS.get(c["kind"], "") if c else "",
                            ("yes" if c["passed"] else "no") if c else ""])
    return path


def write_outputs(res, figures=True, svg_backends=None):
    """Plots, markdown, HTML, CSV, JSON and the Word record into res.out_dir."""
    from . import products
    res.files["plots"] = write_plots(res)
    md_path = os.path.join(res.out_dir, "Verification_Summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(summary_markdown(res))
    page = products.markdown_to_html(summary_markdown(res, plot_marks=True),
                                     "Verification of the hydraulic model codes")
    for cid, p in res.files["plots"].items():
        with open(p, encoding="utf-8") as f:
            svg = f.read()
        page = page.replace("<p>@@PLOT:{}@@</p>".format(cid), '<div class="plot">{}</div>'
                            .format(svg))
    html_path = os.path.join(res.out_dir, "Verification_Summary.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page)
    csv_path = write_csv(res, os.path.join(res.out_dir, "Verification_Metrics.csv"))
    json_path = os.path.join(res.out_dir, "Verification_Results.json")
    res.files.update({"markdown": md_path, "html": html_path, "csv": csv_path,
                      "json": json_path, "work": os.path.join(res.out_dir, "work")})
    docx_path = os.path.join(res.out_dir, "Verification Record.docx")
    write_record(res, docx_path, figures=figures, svg_backends=svg_backends)
    res.files["docx"] = docx_path
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(res.as_dict(), f, indent=1)
    return res.files


def _status_cell(status):
    return {"text": status, "shade": STATUS_SHADES.get(status), "bold": True, "align": "center"}


def _signoff(doc):
    doc.table([["", "Name and title", "Signature", "Date"],
               [{"text": "Prepared by", "bold": True}, "\n\n", "", ""],
               [{"text": "Reviewed by", "bold": True}, "\n\n", "", ""]],
              widths=[1.2, 2.3, 2.0, 1.0], font_pt=10)


def write_record(res, path, figures=True, svg_backends=None, width_px=1400):
    """The Word 'Verification Record' (report_docx.Docx). Plots are embedded as PNG when an
    SVG converter works (report_docx.svg_to_png), else referenced by file name."""
    from . import report_docx as R
    date = res.started or datetime.datetime.now().replace(microsecond=0).isoformat(sep=" ")
    host = dict(res.info).get("Computer", "")
    doc = R.Docx(title="Verification Record", author="",
                 subject="Verification of the hydraulic model codes against known solutions",
                 keywords="verification, dam break, LISFLOOD-FP, ANUGA",
                 header_left="ND Dam Breach Runner", header_right="Verification Record",
                 footer_left=date[:10], number_headings=True)
    exe = res.executables.get("lisflood", {}).get("path", "") or "not given"
    py = res.executables.get("anuga_python", {}).get("path", "") or "not tested"
    doc.title_block(
        "Verification Record",
        "Hydraulic model codes against known solutions, on this computer",
        [("Date", date), ("Computer", host), ("Plugin version", _plugin_version()),
         ("LISFLOOD-FP", exe), ("ANUGA", py),
         ("Mode", "quick (coarser grids and meshes)" if res.quick else "full"),
         ("Result", res.counts_text())],
        note="Verification shows that the installed codes solve their equations as expected on "
             "this computer. It does not validate a model of a particular dam; see the "
             "Validation Record for a comparison with an approved study.")
    doc.toc()
    doc.page_break()
    doc.heading("Purpose and scope", 1)
    doc.paragraph(
        "This record documents that the hydraulic model codes used by the ND Dam Breach Runner "
        "plugin, as installed on the computer named below, reproduce known solutions of the "
        "shallow water equations. Each case runs a small synthetic problem through the model "
        "exactly as the plugin runs it and compares the result with an analytic solution or a "
        "conservation law. The model input files of every run are kept in the work folder "
        "next to this record.")
    doc.paragraph(
        "The acceptance criteria are this tool's own, stated with their basis for each case; "
        "they are not a regulatory standard. Accuracy criteria apply where the scheme is "
        "expected to reproduce the solution. Expected-behaviour criteria apply where the "
        "literature says it cannot (LISFLOOD-FP's local inertial solver in supercritical "
        "flow): they detect a broken or different build and quantify the known limitation, "
        "they do not claim accuracy.")
    doc.heading("Summary of results", 1)
    rows = [["#", "Case", "Model", "Result", "Key measures", "Time (s)"]]
    for i, k in enumerate(res.cases, 1):
        rows.append([str(i), k["name"], k["model"], _status_cell(k["status"]), k["summary"],
                     "{:.1f}".format(k["runtime_s"])])
    doc.table(rows, widths=[0.3, 2.0, 1.0, 0.7, 3.2, 0.6], align=[
        "center", None, None, "center", None, "right"], caption="Verification cases")
    c = res.counts()
    if c[FAIL] or c[ERROR]:
        doc.callout("{} case(s) failed and {} could not run. Do not rely on the affected model "
                    "until the cause is found; see each case below.".format(c[FAIL], c[ERROR]),
                    kind="warn")
    elif c[SKIP]:
        doc.callout("{} case(s) were not run (reason given with each case).".format(c[SKIP]))
    doc.heading("Computer and software", 1)
    doc.table([["Item", "Value"]] + [[a.strip(), b] for a, b in res.info],
              widths=[1.8, 4.7], font_pt=8.5, first_col_bold=True)
    embedded = 0
    for i, k in enumerate(res.cases, 1):
        doc.heading("{} ({})".format(k["name"], k["model"]), 1, page_break_before=True)
        doc.table([[{"text": "Result", "bold": True}, _status_cell(k["status"]),
                    k["summary"] or ""]], header=False, widths=[0.8, 0.8, 4.9], font_pt=10)
        if k.get("error"):
            doc.callout(("Not run: " if k["status"] == SKIP else "Error: ") + k["error"],
                        kind="note" if k["status"] == SKIP else "warn")
        if k.get("description"):
            doc.paragraph(k["description"])
        if k.get("setup"):
            doc.heading("Set-up", 2)
            doc.table([["Item", "Value"]] + [[a, b] for a, b in k["setup"]], widths=[2.0, 4.5],
                      font_pt=9)
        if k.get("reference"):
            doc.heading("Reference solution", 2)
            doc.paragraph(k["reference"])
        if k["criteria"]:
            doc.heading("Acceptance criteria and results", 2)
            rows = [["Criterion", "Measured", "Limit", "Type", "Result"]]
            for cr in k["criteria"]:
                rows.append([cr["label"], _fmt(cr["value"]), cr["text"],
                             KIND_LABELS.get(cr["kind"], cr["kind"]),
                             _status_cell(PASS if cr["passed"] else FAIL)])
            doc.table(rows, widths=[2.9, 0.9, 1.0, 1.0, 0.7],
                      align=[None, "right", "center", None, "center"])
            bases = []
            for cr in k["criteria"]:
                if cr["basis"] and cr["basis"] not in bases:
                    bases.append(cr["basis"])
            doc.paragraph("Basis of the criteria:", bold=True, keep_next=True)
            doc.bullets(bases)
        pt = k.get("per_time")
        if pt:
            doc.table([pt["columns"]] + [[_fmt(v) for v in r] for r in pt["rows"]],
                      align=["right"] * len(pt["columns"]),
                      caption="{}: detail".format(k["name"]))
        if k["metrics"]:
            doc.heading("All measures", 2)
            doc.table([["Measure", "Value"]] + [[k["labels"].get(key, key), _fmt(v)]
                                                for key, v in k["metrics"].items()],
                      widths=[5.0, 1.5], align=[None, "right"], font_pt=8.5)
        if k.get("notes"):
            doc.heading("Findings", 2)
            doc.bullets(list(k["notes"]))
        if k.get("plot_svg"):
            cap = (k.get("arrays") or {}).get("title") or k["name"]
            png = None
            if figures:
                png = R.svg_to_png(k["plot_svg"], k["plot_svg"][:-4] + ".png", width_px,
                                   backends=svg_backends)
            if png:
                try:
                    doc.image(png, width_in=min(6.5, doc.text_width_in), caption=cap)
                    k["plot_png"] = png
                    embedded += 1
                except (ValueError, OSError):
                    png = None
            if not png:
                doc.paragraph("Figure: {} (not embedded: no SVG converter was available; the "
                              "plot is in the file named).".format(
                                  os.path.relpath(k["plot_svg"], res.out_dir)), italic=True)
    doc.heading("How to read this record", 1, page_break_before=True)
    doc.bullets([
        "PASS: every criterion of the case was met. FAIL: at least one was not. ERROR: the "
        "model could not be run (the message says why). SKIP: not run (e.g. no ANUGA "
        "environment).",
        "Verification is about the codes, not a dam: it cannot show that the terrain, "
        "roughness, breach hydrograph or boundaries of a particular study are right. The "
        "Validation Record documents a comparison of a dam's results with an approved study.",
        "LISFLOOD-FP's local inertial solver is expected to be inaccurate in supercritical "
        "flow (the dry-bed and shallow-tailwater dam breaks). Those cases document how large "
        "the effect is on this installation; close to a breach, where the flow is fastest, "
        "LISFLOOD-FP arrival times are late and ANUGA is the better tool.",
        "Quick mode uses coarser grids and meshes than full mode; both use the same criteria."])
    refs = []
    for k in res.cases:
        for r in k.get("references") or []:
            if r not in refs and r in REFS:
                refs.append(r)
    if refs:
        doc.heading("References", 1)
        doc.bullets([REFS[r] for r in refs])
    doc.heading("Sign-off", 1)
    _signoff(doc)
    doc.save(path)
    res.files["figures_embedded"] = embedded
    return path


# ======================================================================================
# validation record from a benchmark folder
# ======================================================================================
BENCH_LABELS = {
    "threshold_ft": ("Wet threshold (ft)", "cells at least this deep count as flooded"),
    "wet_acres_ours": ("Flooded area, this tool (ac)", ""),
    "wet_acres_reference": ("Flooded area, reference study (ac)", ""),
    "wet_acres_both": ("Flooded by both (ac)", ""),
    "fit_index_F": ("Fit index F = A∩B / A∪B", "1 = identical extents; above about 0.8 "
                    "is a close match (Bates & De Roo 2000)"),
    "hit_rate": ("Hit rate", "share of the reference's flooded area this tool also floods"),
    "false_alarm_ratio": ("False-alarm ratio", "share of this tool's flooded area the "
                          "reference does not flood"),
    "area_bias_pct": ("Flooded-area bias (%)", "this tool minus the reference, relative to the "
                      "reference"),
    "depth_class_agreement": ("Same depth class", "share of the cells both flood that fall in "
                              "the same depth class"),
    "depth_class_within_one": ("Within one depth class", ""),
}
BENCH_GROUPS = {"depth_ft": "Depth (ft)", "wse_ft": "Water surface elevation (ft)",
                "velocity_fps": "Velocity (ft/s)", "arrival_hr": "Arrival time (h)"}
BENCH_STATS = {"n": "cells compared", "bias": "mean difference (this tool - reference)",
               "rmse": "RMSE", "mae": "mean absolute difference",
               "p90_abs": "90th percentile of the absolute difference",
               "ratio_mean": "ratio of the means (this tool / reference)"}


def _num_or_text(s):
    try:
        v = float(s)
        return int(v) if re.match(r"^-?\d+$", str(s).strip()) else v
    except (TypeError, ValueError):
        return s


class _Tables(object):
    """Tables of an HTML page as lists of rows of cell texts (standard library only)."""

    def __init__(self, text):
        from html.parser import HTMLParser
        tables, stack = [], []

        class P(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag == "table":
                    stack.append([])
                elif tag == "tr" and stack:
                    stack[-1].append([])
                elif tag in ("td", "th") and stack and stack[-1]:
                    stack[-1][-1].append("")

            def handle_endtag(self, tag):
                if tag == "table" and stack:
                    tables.append(stack.pop())

            def handle_data(self, data):
                if stack and stack[-1] and stack[-1][-1]:
                    stack[-1][-1][-1] += data

        P(convert_charrefs=True).feed(text)
        self.tables = [[[c.strip() for c in r] for r in t if r] for t in tables]


def read_benchmark_folder(folder):
    """What benchmark.write_outputs wrote: {'metrics' {key: value}, 'verdict', 'matrix'
    ({'classes', 'rows'} or None), 'rasters' [fingerprint], 'files' [fingerprint],
    'warnings'}. Raises FileNotFoundError when Benchmark_Summary.csv is missing."""
    csv_path = os.path.join(folder, "Benchmark_Summary.csv")
    if not os.path.isfile(csv_path):
        raise FileNotFoundError("No Benchmark_Summary.csv in {} (run 'Compare with a reference "
                                "model' first)".format(folder))
    metrics, warnings = {}, []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) >= 2 and row[0] != "metric":
                metrics[row[0]] = _num_or_text(row[1])
    verdict = str(metrics.pop("verdict", "") or "")
    matrix = None
    html_path = os.path.join(folder, "Benchmark_Report.html")
    if os.path.isfile(html_path):
        with open(html_path, encoding="utf-8", errors="replace") as f:
            tabs = _Tables(f.read()).tables
        for t in tabs:
            if len(t) >= 2 and t[0] and t[0][0] == "" and len(t[0]) >= 3:
                try:
                    matrix = {"classes": t[0][1:],
                              "rows": [[int(float(v)) for v in r[1:]] for r in t[1:]],
                              "row_classes": [r[0] for r in t[1:]]}
                except ValueError:
                    matrix = None
        if matrix is None:
            warnings.append("Benchmark_Report.html holds no depth-class matrix (no cells were "
                            "flooded by both models, or the file was edited).")
    else:
        warnings.append("Benchmark_Report.html is missing, so the depth-class matrix could not "
                        "be read.")
    rasters, files = [], []
    for fn in sorted(os.listdir(folder)):
        p = os.path.join(folder, fn)
        if not os.path.isfile(p):
            continue
        fp = file_fingerprint(p)
        fp["name"] = fn
        if fn.lower().endswith((".tif", ".tiff")):
            rasters.append(fp)
        elif fn in ("Benchmark_Summary.csv", "Benchmark_Report.html"):
            files.append(fp)
    if not rasters:
        warnings.append("No rasters (.tif) in the benchmark folder.")
    return {"metrics": metrics, "verdict": verdict, "matrix": matrix, "rasters": rasters,
            "files": files, "warnings": warnings, "folder": os.path.abspath(folder)}


def guess_dam_id(folder):
    """The dam folder's name for <dam>/_benchmark or <dam>/_hecras/benchmark, else the
    folder's own name."""
    parts = [p for p in os.path.normpath(os.path.abspath(folder)).split(os.sep) if p]
    if len(parts) >= 2 and parts[-1].lower() in ("_benchmark", "benchmark"):
        if len(parts) >= 3 and parts[-2].lower() == "_hecras":
            return parts[-3]
        return parts[-2]
    return parts[-1] if parts else ""


def _bench_rows(metrics):
    rows, seen = [], set()
    for key in ("threshold_ft", "wet_acres_ours", "wet_acres_reference", "wet_acres_both",
                "fit_index_F", "hit_rate", "false_alarm_ratio", "area_bias_pct",
                "depth_class_agreement", "depth_class_within_one"):
        if key in metrics:
            lab, mean = BENCH_LABELS[key]
            rows.append([lab, _fmt(metrics[key]), mean])
            seen.add(key)
    for g, glabel in BENCH_GROUPS.items():
        for s, slabel in BENCH_STATS.items():
            key = g + "." + s
            if key in metrics:
                rows.append(["{}: {}".format(glabel, slabel), _fmt(metrics[key]), ""])
                seen.add(key)
    for key, v in metrics.items():
        if key not in seen:
            rows.append([key, _fmt(v), ""])
    return rows


def validation_record(benchmark_folder, out_path, dam_id="", reference_description="",
                      reference_model="HEC-RAS 2D", study_title="", study_by="", study_date="",
                      approval="", reference_version="", this_model="", dam_name="",
                      acceptance_statement="", notes=None):
    """Word 'Validation Record' of a comparison with an approved study, from a benchmark
    folder written by benchmark.write_outputs (Benchmark_Summary.csv, Benchmark_Report.html,
    the difference and agreement rasters). Study fields left empty are left blank in the
    record to be filled in by hand; so is the acceptance statement when not given.
    Returns {'path', 'metrics', 'verdict', 'matrix', 'rasters', 'warnings'}."""
    from . import report_docx as R
    b = read_benchmark_folder(benchmark_folder)
    dam_id = dam_id or guess_dam_id(benchmark_folder)
    m = b["metrics"]
    today = datetime.date.today().isoformat()
    blank = ""
    doc = R.Docx(title="Validation Record", subject="Comparison with an approved study",
                 keywords="validation, benchmark, " + reference_model,
                 header_left="ND Dam Breach Runner", header_right="Validation Record: " + dam_id,
                 footer_left=today, number_headings=True)
    doc.title_block("Validation Record",
                    "{}{}: comparison with {}".format(dam_id, " ({})".format(dam_name)
                                                      if dam_name else "", reference_model),
                    [("Dam ID", dam_id), ("Dam name", dam_name), ("Reference model",
                                                                 reference_model),
                     ("Reference study", study_title), ("Study date", study_date),
                     ("Record date", today), ("Benchmark folder", b["folder"])],
                    note="This record compares this tool's breach flood results with an approved "
                         "study of the same dam. It complements the Verification Record, which "
                         "shows that the model codes reproduce known solutions.")
    doc.heading("Purpose", 1)
    doc.paragraph(
        "To document how closely this tool's results for dam {} agree with an approved "
        "{} study, so a reviewer can decide whether the tool's results are acceptable for the "
        "intended use (for example hazard classification or EAP inundation mapping). Both "
        "models' rasters were compared on the same grid by the tool's benchmark "
        "(Benchmark_Summary.csv and Benchmark_Report.html in the benchmark folder).".format(
            dam_id, reference_model))
    doc.heading("Study identification", 1)
    doc.table([["Item", "Entry"],
               ["Reference model and version", " ".join(x for x in (reference_model,
                                                                    reference_version) if x)],
               ["Study title / report", study_title or blank],
               ["Prepared by (firm, engineer)", study_by or blank],
               ["Study date", study_date or blank],
               ["Approval (agency, reference, date)", approval or blank],
               ["Description of the reference results", reference_description or blank],
               ["This tool's run (tier, cell size, breach method, date)", this_model or blank],
               ["Wet threshold used in the comparison (ft)", _fmt(m.get("threshold_ft"))]],
              widths=[2.4, 4.1], first_col_bold=True, font_pt=9.5)
    doc.heading("Comparison", 1)
    if b["verdict"]:
        doc.paragraph([{"text": "Benchmark verdict: ", "b": True},
                       {"text": b["verdict"].rstrip(".") + "."}])
    doc.table([["Measure", "Value", "Meaning"]] + _bench_rows(m), widths=[2.6, 0.9, 3.0],
              align=[None, "right", None], font_pt=8.5, caption="Comparison metrics")
    doc.heading("Depth-class agreement", 1)
    mx = b["matrix"]
    if mx:
        n = len(mx["classes"])
        rows = [["This tool ↓ / reference →"] + list(mx["classes"])]
        for i, r in enumerate(mx["rows"]):
            label = mx.get("row_classes", mx["classes"])[i] if i < n else ""
            rows.append([{"text": label, "bold": True}] + [
                {"text": "{:,}".format(v), "shade": "E2EFDA" if j == i else None,
                 "align": "right"} for j, v in enumerate(r)])
        doc.table(rows, widths=[2.0] + [1.1] * n, caption="Cells flooded by both models, by "
                  "depth class (ft); the diagonal is agreement")
        tot = sum(sum(r) for r in mx["rows"])
        diag = sum(mx["rows"][i][i] for i in range(min(n, len(mx["rows"]))))
        doc.paragraph("{:,} cells flooded by both; {:,} ({:.1%}) in the same class{}.".format(
            tot, diag, diag / float(tot) if tot else 0.0,
            "; {:.1%} within one class".format(float(m["depth_class_within_one"]))
            if isinstance(m.get("depth_class_within_one"), (int, float)) else ""))
    else:
        doc.paragraph("Not available: " + " ".join(w for w in b["warnings"]
                                                   if "matrix" in w), italic=True)
    doc.heading("Rasters and files", 1)
    frows = [["File", "Size (bytes)", "SHA-256"]]
    for fp in b["rasters"] + b["files"]:
        frows.append([fp["name"], "{:,}".format(fp["size"] or 0), fp["sha256"]])
    doc.table(frows, widths=[2.2, 0.9, 3.4], font_pt=7.5, align=[None, "right", None])
    doc.paragraph("Agreement raster: 1 = only this tool floods the cell, 2 = only the reference, "
                  "3 = both. Depth difference raster: this tool minus the reference (ft).",
                  italic=True)
    for w in b["warnings"]:
        doc.callout(w, kind="warn")
    for note in notes or []:
        doc.callout(note)
    doc.heading("Acceptance", 1)
    if acceptance_statement:
        doc.paragraph(acceptance_statement)
    else:
        doc.callout("To be completed by the reviewing engineer: state whether the agreement is "
                    "acceptable for the intended use, with the reasons (for example where and "
                    "why the extents or depths differ, and whether the differences change the "
                    "hazard classification or the EAP inundation map).")
        doc.table([[""], ["\n\n\n\n"]], header=False, widths=[6.5])
    doc.paragraph("☐ Accepted      ☐ Accepted with conditions      ☐ Not accepted",
                  space_before=120)
    doc.heading("References", 1)
    doc.bullets([REFS["bates_deroo"]])
    doc.heading("Sign-off", 1)
    _signoff(doc)
    doc.save(out_path)
    return {"path": out_path, "metrics": m, "verdict": b["verdict"], "matrix": mx,
            "rasters": [fp["name"] for fp in b["rasters"]], "warnings": b["warnings"],
            "dam_id": dam_id}
