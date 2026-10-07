"""Probabilistic (Monte Carlo) breach runs around a base breach model.

The sensitivity tool answers "how much does it change between low and high?".
This one answers "how likely is it?": the breach width, formation time and
Manning's n are drawn from distributions (factors on the base run's values),
optionally together with the breach parameter method, and every draw is run
with LISFLOOD-FP on the base run's domain, inflow point and simulation time
(the same fixed plan the sensitivity cases use), so the rasters line up cell
for cell. The result is, for each cell, the probability that the depth reaches
a threshold, the 10th / 50th / 90th percentile depth, the 90th percentile
velocity and the earliest plausible arrival.

Sampling is Latin hypercube with numpy's Generator (PCG64) and a seed; the seed
and every sample are written to `probabilistic_manifest.json`, so the study can
be repeated exactly. Each run's depth, velocity and arrival are written as
float32 .npy files in `Probabilistic/_work/`; the per-cell statistics are
computed from those files a block of rows at a time, so memory does not grow
with the number of runs. A study that was interrupted continues where it
stopped: a run whose files exist for the same sample and the same base run is
not run again.

What is NOT varied: the pool level at failure. The breach module has no input
for it in the sunny-day case (the starting level is the top of dam, or the PMF
maximum level when lower), and in the flood scenario it follows from the
failure trigger.

The default distributions are adjustable placeholders for a first look, not
published values.

v3.2.0, option "Write each run's rasters for the class probability" (default off): every
finished run also gets `Probabilistic/Runs/run_NNN/` with `Depth (Max).tif` (ft),
`Velocity (Max).tif` (ft/s) and `Arrival Time.tif` (hours after breach initiation), written
from the run's _work arrays with the same writer (grid and CRS) as the percentile rasters,
and `run.json` (the sample, its equal weight, the run key, units, and a reference to the base
run's `Baseline ...` rasters in the dam folder when the scenario has a baseline - they are
not copied). `Probabilistic/Runs/index.json` lists the finished runs. ND Dam Hazard's class
probability tool classifies these folders. A run whose rasters exist for the same key is not
written again; folders of runs that failed or are no longer part of the study lose their
run.json and rasters.
"""
import copy
import csv
import dataclasses
import datetime
import json
import math
import os
import re
import statistics
import time

import numpy as np

from . import breach as brmod
from . import impacts as impmod
from . import lisflood as lfmod
from . import sensitivity as sensmod
from . import workflow
from .units import FT, ACRE

FOLDER = "Probabilistic"
WORK_FOLDER = "_work"
RUNS_FOLDER = "Runs"         # v3.2.0 (was "runs": the same folder on Windows and macOS)
LEGACY_RUNS_FOLDER = "runs"
# v3.2.0: per-run rasters for ND Dam Hazard's class probability (option, default off)
RUN_KINDS = ("depth", "velocity", "arrival")
RUN_JSON = "run.json"
RUNS_INDEX = "index.json"
RUN_FORMAT = "ndbreach/probabilistic_run"
RUNS_INDEX_FORMAT = "ndbreach/probabilistic_runs"
RUN_UNITS = {"depth": "ft", "velocity": "ft/s", "arrival": "hours after breach initiation"}
WEIGHT_NOTE = ("Equal weights: with Latin hypercube sampling every finished run stands for the "
               "same probability (1 / the number of finished runs). The breach-method weights "
               "act through how often each method was drawn, not through the run weights.")
DEFAULT_N = 25
MAX_RUNS = 200
MIN_RUNS = 2
DEFAULT_SEED = 12345
DEFAULT_THRESHOLDS_FT = (1.0, 3.0)
DEPTH_Q = (10.0, 50.0, 90.0)
VELOCITY_Q = 90.0
ARRIVAL_Q = 10.0
MIN_REACHED = 0.1            # arrival percentile only where at least 10% of the runs reach the cell
BLOCK_BYTES = 24e6           # one run-stack of float32 per block of rows (a few copies are made)
LOGN_TAIL = 1e-4             # lognormal draws are truncated at the 0.01 / 99.99 percentiles
KINDS = ("depth", "velocity", "arrival")
GROUP = "probabilistic"

DIST_TRI, DIST_UNIFORM, DIST_LOGNORMAL, DIST_FIXED = "triangular", "uniform", "lognormal", "fixed"
_DIST_PARAMS = {DIST_TRI: ("min", "mode", "max"), DIST_UNIFORM: ("min", "max"),
                DIST_LOGNORMAL: ("median", "sigma"), DIST_FIXED: ("value",)}
_DIST_ALIASES = {"tri": DIST_TRI, "triangular": DIST_TRI, "triang": DIST_TRI,
                 "triangle": DIST_TRI, "uni": DIST_UNIFORM, "unif": DIST_UNIFORM,
                 "uniform": DIST_UNIFORM, "logn": DIST_LOGNORMAL, "lognormal": DIST_LOGNORMAL,
                 "log-normal": DIST_LOGNORMAL, "lnorm": DIST_LOGNORMAL, "ln": DIST_LOGNORMAL,
                 "fixed": DIST_FIXED, "const": DIST_FIXED, "constant": DIST_FIXED}
_OFF = ("none", "off", "no", "not varied", "-")

DEFAULTS_NOTE = ("The default distributions (breach width x0.5 / x1.0 / x1.5, formation time "
                 "x0.5 / x1.0 / x2.0, Manning's n x0.75 / x1.0 / x1.3, all triangular min / mode "
                 "/ max factors on the base run's values) are adjustable placeholders for a "
                 "first look, NOT published values. Set them from the breach-parameter "
                 "uncertainty you can defend (for example the prediction intervals of the "
                 "regression you used) and the roughness range of the valley.")
POOL_NOTE = ("The pool level at failure is not varied: the breach module has no pool-level "
             "input for the sunny-day case (the starting level is the top of dam, or the PMF "
             "maximum level when lower), and in the flood scenario it follows from the "
             "failure trigger.")


# ----------------------------------------------------------------------------------------
# distributions
# ----------------------------------------------------------------------------------------
def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        raise ValueError("'{}' is not a number".format(s))


class Dist:
    """A distribution of a factor: triangular(min, mode, max), uniform(min, max),
    lognormal(median, sigma of ln x) or fixed(value)."""

    def __init__(self, kind, *params):
        k = _DIST_ALIASES.get(str(kind).strip().lower())
        if k is None:
            raise ValueError("unknown distribution '{}' (use tri, uniform, lognormal or "
                             "fixed)".format(kind))
        names = _DIST_PARAMS[k]
        if len(params) != len(names):
            raise ValueError("{} needs {} value{} ({}), got {}".format(
                k, len(names), "" if len(names) == 1 else "s", ", ".join(names),
                len(params)))
        self.kind = k
        self.params = tuple(_num(p) for p in params)
        self._check()

    def _check(self):
        p = self.params
        if not all(math.isfinite(x) for x in p):
            raise ValueError("{}: the values must be finite".format(self.describe()))
        if self.kind == DIST_TRI:
            a, c, b = p
            if not a <= c <= b:
                raise ValueError("triangular needs min <= mode <= max (got {:g}, {:g}, {:g})"
                                 .format(a, c, b))
            lo = a
        elif self.kind == DIST_UNIFORM:
            a, b = p
            if not a <= b:
                raise ValueError("uniform needs min <= max (got {:g}, {:g})".format(a, b))
            lo = a
        elif self.kind == DIST_LOGNORMAL:
            if p[1] < 0:
                raise ValueError("lognormal sigma must be 0 or more (got {:g})".format(p[1]))
            lo = p[0]
        else:
            lo = p[0]
        if lo <= 0:
            raise ValueError("{}: a factor must stay above 0".format(self.describe()))

    # -- numbers ----------------------------------------------------------------------
    def ppf(self, u):
        """Inverse CDF: the factor at cumulative probability u (array)."""
        u = np.clip(np.asarray(u, dtype=float), 0.0, 1.0)
        p = self.params
        if self.kind == DIST_TRI:
            a, c, b = p
            if b == a:
                return np.full(u.shape, a)
            fc = (c - a) / (b - a)
            left = a + np.sqrt(u * (b - a) * (c - a))
            right = b - np.sqrt((1.0 - u) * (b - a) * (b - c))
            return np.where(u < fc, left, right)
        if self.kind == DIST_UNIFORM:
            return p[0] + u * (p[1] - p[0])
        if self.kind == DIST_LOGNORMAL:
            nd = statistics.NormalDist()
            uu = np.clip(u, LOGN_TAIL, 1.0 - LOGN_TAIL)
            z = np.array([nd.inv_cdf(float(x)) for x in uu.ravel()]).reshape(uu.shape)
            return p[0] * np.exp(p[1] * z)
        return np.full(u.shape, p[0])

    def cdf(self, x):
        x = np.asarray(x, dtype=float)
        p = self.params
        if self.kind == DIST_TRI:
            a, c, b = p
            if b == a:
                return (x >= a).astype(float)
            left = (x - a) ** 2 / ((b - a) * (c - a)) if c > a else np.ones(x.shape)
            right = 1.0 - (b - x) ** 2 / ((b - a) * (b - c)) if b > c else np.ones(x.shape)
            return np.clip(np.where(x <= a, 0.0, np.where(x < c, left, np.where(
                x < b, right, 1.0))), 0.0, 1.0)
        if self.kind == DIST_UNIFORM:
            a, b = p
            if b == a:
                return (x >= a).astype(float)
            return np.clip((x - a) / (b - a), 0.0, 1.0)
        if self.kind == DIST_LOGNORMAL:
            nd = statistics.NormalDist()
            if p[1] == 0:
                return (x >= p[0]).astype(float)
            z = np.log(np.clip(x, 1e-300, None) / p[0]) / p[1]
            return np.array([nd.cdf(float(v)) for v in z.ravel()]).reshape(z.shape)
        return (x >= p[0]).astype(float)

    def varies(self):
        if self.kind == DIST_FIXED:
            return False
        if self.kind == DIST_LOGNORMAL:
            return self.params[1] > 0
        return self.params[0] != self.params[-1]

    # -- text and dicts ---------------------------------------------------------------
    def describe(self):
        names = _DIST_PARAMS[self.kind]
        if self.kind == DIST_FIXED:
            return "fixed at {:g}".format(self.params[0])
        return "{} ({})".format(self.kind, ", ".join("{} {:g}".format(n, v) for n, v in
                                                     zip(names, self.params)))

    def to_text(self):
        short = {DIST_TRI: "tri", DIST_UNIFORM: "uniform", DIST_LOGNORMAL: "lognormal",
                 DIST_FIXED: "fixed"}[self.kind]
        return " ".join([short] + ["{:g}".format(v) for v in self.params])

    def to_dict(self):
        d = {"dist": self.kind}
        d.update(zip(_DIST_PARAMS[self.kind], self.params))
        return d

    @classmethod
    def from_dict(cls, d):
        if d is None:
            return cls(DIST_FIXED, 1.0)
        if isinstance(d, Dist):
            return d
        if isinstance(d, (int, float)):
            return cls(DIST_FIXED, d)
        if isinstance(d, str):
            r = cls.from_text(d)
            return r if r is not None else cls(DIST_FIXED, 1.0)
        kind = d.get("dist", d.get("kind"))
        if kind is None:
            raise ValueError("a distribution needs 'dist' (tri, uniform, lognormal or fixed)")
        k = _DIST_ALIASES.get(str(kind).strip().lower())
        if k is None:
            raise ValueError("unknown distribution '{}'".format(kind))
        if "params" in d:
            return cls(k, *d["params"])
        missing = [n for n in _DIST_PARAMS[k] if n not in d]
        if missing:
            raise ValueError("{} needs {}".format(k, ", ".join(_DIST_PARAMS[k])))
        return cls(k, *[d[n] for n in _DIST_PARAMS[k]])

    @classmethod
    def from_text(cls, text):
        """'tri 0.5 1 1.5', 'uniform 0.5, 2', 'lognormal 1 0.2', 'fixed 1.2', 'off'
        (= fixed 1), or just numbers (3 = triangular, 2 = uniform, 1 = fixed). An
        optional 'name:' prefix is ignored. Empty text gives None."""
        s = str(text or "").strip()
        if ":" in s:
            s = s.split(":", 1)[1].strip()
        if not s:
            return None
        if s.lower() in _OFF:
            return cls(DIST_FIXED, 1.0)
        tok = [t for t in re.split(r"[,\s()]+", s) if t]
        try:
            nums = [float(t) for t in tok]
        except ValueError:
            nums = None
        if nums is not None:
            kind = {3: DIST_TRI, 2: DIST_UNIFORM, 1: DIST_FIXED}.get(len(nums))
            if kind is None:
                raise ValueError("'{}': give 1 (fixed), 2 (uniform) or 3 (triangular) "
                                 "numbers, or name the distribution".format(text))
            return cls(kind, *nums)
        return cls(tok[0], *tok[1:])

    def __eq__(self, other):
        return isinstance(other, Dist) and self.kind == other.kind and \
            self.params == other.params

    def __repr__(self):
        return "Dist({!r})".format(self.to_text())


def default_width():
    return Dist(DIST_TRI, 0.5, 1.0, 1.5)


def default_time():
    return Dist(DIST_TRI, 0.5, 1.0, 2.0)


def default_manning():
    return Dist(DIST_TRI, 0.75, 1.0, 1.3)


# ----------------------------------------------------------------------------------------
# breach methods to draw from
# ----------------------------------------------------------------------------------------
def _norm_key(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


_METHOD_ALIASES = {_norm_key(m): m for m in brmod.METHODS if m != brmod.METHOD_USER}
_METHOD_ALIASES.update({
    "froehlich2008piping": brmod.METHOD_PIPING, "f08piping": brmod.METHOD_PIPING,
    "froehlich2008overtopping": brmod.METHOD_OVERTOPPING,
    "f08overtopping": brmod.METHOD_OVERTOPPING, "orifice": brmod.METHOD_PIPING_ORIFICE,
    "f95piping": brmod.METHOD_F95_PIPING, "f95overtopping": brmod.METHOD_F95_OVERTOPPING,
    "mlm": brmod.METHOD_MLM, "macdonald": brmod.METHOD_MLM, "vtg": brmod.METHOD_VTG,
    "vonthun": brmod.METHOD_VTG, "tr60triangular": brmod.METHOD_TR60,
    "tr60curve": brmod.METHOD_TR60_CURVE,
})


def method_id(name):
    m = _METHOD_ALIASES.get(_norm_key(name))
    if m is None:
        if _norm_key(name) == brmod.METHOD_USER:
            raise ValueError("'user' (your own hydrograph) cannot be drawn: it has no breach "
                             "parameters to vary")
        raise ValueError("unknown breach method '{}' (use {})".format(
            name, ", ".join(m for m in brmod.METHODS if m != brmod.METHOD_USER)))
    return m


def parse_methods(text):
    """'piping 0.6, overtopping 0.4' (weights optional, default 1) -> [(method, weight)]."""
    out = []
    for item in re.split(r"[,;\n]+", str(text or "")):
        tok = [t for t in re.split(r"[\s:=()]+", item.strip()) if t]
        if not tok:
            continue
        w = 1.0
        if len(tok) > 1:
            try:
                w = float(tok[-1])
                tok = tok[:-1]
            except ValueError:
                pass
        m = method_id("_".join(tok))
        if not (w > 0 and math.isfinite(w)):
            raise ValueError("the weight of {} must be above 0".format(m))
        out.append((m, w))
    seen = set()
    for m, _w in out:
        if m in seen:
            raise ValueError("breach method {} is listed twice".format(m))
        seen.add(m)
    return out


# ----------------------------------------------------------------------------------------
# the specification
# ----------------------------------------------------------------------------------------
_FACTOR_KEYS = {"width": "width", "breachwidth": "width", "w": "width", "avgwidth": "width",
                "time": "time", "formationtime": "time", "t": "time", "tf": "time",
                "formation": "time", "n": "manning", "manning": "manning",
                "manningsn": "manning", "manningn": "manning", "roughness": "manning",
                "methods": "methods", "method": "methods"}
FACTOR_LABELS = {"width": "Breach width", "time": "Formation time", "manning": "Manning's n"}


@dataclasses.dataclass
class ProbSpec:
    """What is uncertain and how. Factors multiply the base run's values; `methods`
    is [(method id, weight)] to draw the breach method from (empty = the base
    run's method in every run)."""
    width: Dist = dataclasses.field(default_factory=default_width)
    time: Dist = dataclasses.field(default_factory=default_time)
    manning: Dist = dataclasses.field(default_factory=default_manning)
    methods: list = dataclasses.field(default_factory=list)

    def validate(self):
        for k in ("width", "time", "manning"):
            d = getattr(self, k)
            if not isinstance(d, Dist):
                setattr(self, k, Dist.from_dict(d))
        ms = []
        for item in self.methods or []:
            if isinstance(item, dict):
                m, w = item.get("method"), item.get("weight", 1.0)
            elif isinstance(item, str):
                m, w = item, 1.0
            else:
                m, w = item
            w = _num(w)
            if not (w > 0 and math.isfinite(w)):
                raise ValueError("the weight of {} must be above 0".format(m))
            ms.append((method_id(m), w))
        if len({m for m, _w in ms}) != len(ms):
            raise ValueError("a breach method is listed twice")
        self.methods = ms
        return self

    def method_choices(self, base_method):
        """[(method, probability)] with the weights normalised."""
        if not self.methods:
            return [(base_method, 1.0)]
        tot = float(sum(w for _m, w in self.methods))
        return [(m, w / tot) for m, w in self.methods]

    def to_dict(self):
        return {"width": self.width.to_dict(), "time": self.time.to_dict(),
                "manning": self.manning.to_dict(),
                "methods": [{"method": m, "weight": w} for m, w in self.methods]}

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        kw = {}
        for k, v in d.items():
            key = _FACTOR_KEYS.get(_norm_key(k))
            if key is None:
                if _norm_key(k).startswith("pool"):
                    raise ValueError(POOL_NOTE)
                raise ValueError("unknown uncertain input '{}' (use width, time, manning, "
                                 "methods)".format(k))
            if key == "methods":
                kw[key] = parse_methods(v) if isinstance(v, str) else list(v or [])
            else:
                try:
                    kw[key] = Dist.from_dict(v)
                except ValueError as e:
                    raise ValueError("{}: {}".format(FACTOR_LABELS[key], e))
        return cls(**kw).validate()

    @classmethod
    def from_text(cls, text):
        """'width: tri 0.5 1 1.5; time: uniform 0.5 2; n: lognormal 1 0.15;
        methods: piping 0.6, overtopping 0.4' (statements separated by ';' or new
        lines; anything not given keeps its default)."""
        d = {}
        for stmt in re.split(r"[;\n]+", str(text or "")):
            if not stmt.strip():
                continue
            if ":" not in stmt:
                raise ValueError("'{}': write it as 'name: distribution' (e.g. 'width: tri "
                                 "0.5 1 1.5')".format(stmt.strip()))
            k, v = stmt.split(":", 1)
            key = _FACTOR_KEYS.get(_norm_key(k))
            if key is None:
                d[k.strip()] = v              # reported by from_dict
                continue
            if key == "methods":
                d[key] = v
            else:
                v = v.strip()
                if not v:
                    raise ValueError("{}: no distribution given".format(FACTOR_LABELS[key]))
                d[key] = v
        return cls.from_dict(d)

    @classmethod
    def from_parts(cls, width="", time="", manning="", methods=""):
        """One text per input, as the Processing tool has them. Empty = not varied."""
        kw = {}
        for key, txt in (("width", width), ("time", time), ("manning", manning)):
            try:
                kw[key] = Dist.from_text(txt) or Dist(DIST_FIXED, 1.0)
            except ValueError as e:
                raise ValueError("{}: {}".format(FACTOR_LABELS[key], e))
        kw["methods"] = parse_methods(methods)
        return cls(**kw).validate()

    def describe(self):
        rows = [(FACTOR_LABELS[k], getattr(self, k).describe()) for k in
                ("width", "time", "manning")]
        rows.append(("Breach method", ", ".join("{} (weight {:g})".format(m, w)
                                                for m, w in self.methods)
                     if self.methods else "the base run's method in every run"))
        return rows


# ----------------------------------------------------------------------------------------
# sampling
# ----------------------------------------------------------------------------------------
def latin_hypercube(n, d, rng):
    """n x d points in [0, 1): in every column each of the n strata [k/n, (k+1)/n)
    holds exactly one point. Column by column: a permutation, then the offsets."""
    u = np.empty((n, d))
    for j in range(d):
        u[:, j] = (rng.permutation(n) + rng.random(n)) / n
    return u


def pick(u, probs):
    """Index into `probs` (which sum to 1) for each u in [0, 1)."""
    c = np.cumsum(np.asarray(probs, dtype=float))
    c[-1] = 1.0
    return np.minimum(np.searchsorted(c, np.asarray(u), side="right"), len(c) - 1)


def draw_samples(spec, n, seed, base_method):
    """The runs of a study: [{run, width_factor, time_factor, n_factor, method, u}].
    Four dimensions are always drawn, in this order: width, time, Manning's n,
    method; so switching one input off does not change the draws of the others."""
    rng = np.random.default_rng(int(seed))
    u = latin_hypercube(int(n), 4, rng)
    w = spec.width.ppf(u[:, 0])
    t = spec.time.ppf(u[:, 1])
    mn = spec.manning.ppf(u[:, 2])
    choices = spec.method_choices(base_method)
    idx = pick(u[:, 3], [p for _m, p in choices])
    return [{"run": i + 1, "width_factor": float(w[i]), "time_factor": float(t[i]),
             "n_factor": float(mn[i]), "method": choices[int(idx[i])][0],
             "u": [float(x) for x in u[i]]} for i in range(int(n))]


# ----------------------------------------------------------------------------------------
# statistics over the runs
# ----------------------------------------------------------------------------------------
def percentile_sorted(s, k, q):
    """Percentile q (numpy's default linear interpolation) of the first k values of
    s along axis 0; s is sorted along axis 0 with NaN last, k an integer array of
    s.shape[1:]. NaN where k == 0."""
    k = np.asarray(k)
    km1 = np.maximum(k - 1, 0)
    pos = (float(q) / 100.0) * km1
    lo = np.floor(pos).astype(np.intp)
    hi = np.minimum(lo + 1, km1).astype(np.intp)
    frac = pos - lo
    a = np.take_along_axis(s, lo[None], axis=0)[0].astype(np.float64)
    b = np.take_along_axis(s, hi[None], axis=0)[0].astype(np.float64)
    with np.errstate(invalid="ignore"):
        v = a + frac * (b - a)
    return np.where(k > 0, v, np.nan)


def block_stats(depth, velocity, arrival, thresholds_m, depth_q=DEPTH_Q,
                velocity_q=VELOCITY_Q, arrival_q=ARRIVAL_Q, min_reached=MIN_REACHED):
    """Statistics over runs for one block. Inputs are (runs, rows, cols), SI, NaN
    where dry (depth, velocity) or not reached (arrival). Dry counts as depth and
    velocity 0; the arrival percentile is over the runs that reach the cell and
    NaN where fewer than `min_reached` of the runs do."""
    n = depth.shape[0]
    d = np.where(np.isfinite(depth), depth, 0.0).astype(np.float32)
    out = {"prob": [(d >= np.float32(t)).sum(axis=0) / float(n) for t in thresholds_m]}
    full = np.full(d.shape[1:], n)
    ds = np.sort(d, axis=0)
    out["depth"] = {q: percentile_sorted(ds, full, q) for q in depth_q}
    del ds
    v = np.sort(np.where(np.isfinite(velocity), velocity, 0.0).astype(np.float32), axis=0)
    out["velocity"] = percentile_sorted(v, full, velocity_q)
    del v
    a = np.asarray(arrival, dtype=np.float32)
    k = np.isfinite(a).sum(axis=0)
    p = percentile_sorted(np.sort(a, axis=0), k, arrival_q)
    out["arrival"] = np.where(k >= min_reached * n - 1e-9, p, np.nan)
    out["reached"] = k
    return out


def blockwise_stats(depth, velocity, arrival, shape, thresholds_m, block_rows=None,
                    block_bytes=BLOCK_BYTES, **kw):
    """block_stats over whole rasters given as one 2-D array (or memmap) per run,
    reading `block_rows` rows of every run at a time."""
    n = len(depth)
    ny, nx = shape
    if block_rows is None:
        block_rows = max(1, int(block_bytes // max(1, n * nx * 4)))
    dq = kw.get("depth_q", DEPTH_Q)
    f32 = np.float32              # full-size results: float32 keeps a big domain in memory
    res = {"prob": [np.zeros(shape, f32) for _t in thresholds_m],
           "depth": {q: np.full(shape, np.nan, f32) for q in dq},
           "velocity": np.full(shape, np.nan, f32), "arrival": np.full(shape, np.nan, f32),
           "reached": np.zeros(shape, np.int32)}
    for r0 in range(0, ny, block_rows):
        r1 = min(ny, r0 + block_rows)
        st = lambda arrs: np.stack([np.asarray(a[r0:r1], dtype=np.float32) for a in arrs])
        b = block_stats(st(depth), st(velocity), st(arrival), thresholds_m, **kw)
        for i in range(len(thresholds_m)):
            res["prob"][i][r0:r1] = b["prob"][i]
        for q in dq:
            res["depth"][q][r0:r1] = b["depth"][q]
        for key in ("velocity", "arrival", "reached"):
            res[key][r0:r1] = b[key]
    return res


# ----------------------------------------------------------------------------------------
# the study
# ----------------------------------------------------------------------------------------
class ProbabilisticResult:
    def __init__(self):
        self.folder = ""
        self.work_folder = ""
        self.runs_folder = ""
        self.seed = None
        self.n = 0
        self.spec = None
        self.samples = []
        self.rows = []               # one per sample (status says whether it counted)
        self.rasters = {}            # file name -> path
        self.runs_csv = ""
        self.summary_csv = ""
        self.receptors_csv = ""
        self.convergence_svg = ""
        self.report_html = ""
        self.report_md = ""
        self.manifest = ""
        self.base = None
        self.warnings = []
        self.flags = []
        self.resumed = 0
        self.completed = 0
        self.estimate_min = None
        self.runs_index = ""         # v3.2.0: Runs/index.json (per-run rasters option)
        self.run_folders = []


RUN_FIELDS = ["run", "status", "resumed", "method", "width_factor", "time_factor", "n_factor",
              "avg_breach_width_ft", "formation_time_hr", "tr60_rise_time_hr", "manning_n",
              "peak_cfs", "flooded_ac",
              "flooded_ac_over_arrival_depth", "max_depth_ft", "max_velocity_fps",
              "latest_arrival_hr", "structures_flooded", "structures_depth_ge_3ft",
              "road_km_flooded", "rail_km_flooded", "hazard_class", "domain_too_small",
              "minutes"]
SUMMARY_FIELDS = ["metric", "unit", "runs", "mean", "std", "min", "p10", "p50", "p90", "max",
                  "base"]
_METRICS = (("peak_cfs", "cfs"), ("flooded_ac", "ac"), ("flooded_ac_over_arrival_depth", "ac"),
            ("max_depth_ft", "ft"), ("max_velocity_fps", "ft/s"), ("latest_arrival_hr", "hr"),
            ("avg_breach_width_ft", "ft"), ("formation_time_hr", "hr"),
            ("width_factor", "x"), ("time_factor", "x"), ("n_factor", "x"),
            ("structures_flooded", "count"), ("structures_depth_ge_3ft", "count"),
            ("road_km_flooded", "km"), ("rail_km_flooded", "km"))
_OK = ("done", "no breach")
_KEY_SKIP = ("resume", "threads", "lisflood_exe", "anuga_python", "products", "keep_model_files",
             "write_final", "keep_arrays", "gauges", "gauge_massint_s", "fixed_plan", "receptors",
             "tier", "baseline", "arrival_contours_hr", "refine_lines", "anuga_fine_area_m2",
             "anuga_coarse_area_m2", "anuga_near_field_mi", "anuga_buffer_m", "anuga_algorithm",
             "impact_look_ahead_mi", "incr_thresholds_ft")


def raster_names(thresholds_ft):
    """Output raster file names (Windows does not allow '>' in file names)."""
    names = {("prob", t): "Probability Depth at least {:g} ft.tif".format(t)
             for t in thresholds_ft}
    names.update({("depth", q): "Depth P{:g} (ft).tif".format(q) for q in DEPTH_Q})
    names[("velocity", VELOCITY_Q)] = "Velocity P{:g} (ft-s).tif".format(VELOCITY_Q)
    names[("arrival", ARRIVAL_Q)] = "Arrival P{:g} (hr).tif".format(ARRIVAL_Q)
    return names


def _thresholds(thresholds_ft):
    t = sorted({float(x) for x in (thresholds_ft or DEFAULT_THRESHOLDS_FT)})
    if not t or any(not (x > 0 and math.isfinite(x)) for x in t):
        raise ValueError("the depth thresholds must be above 0 ft (got {})".format(
            thresholds_ft))
    return tuple(t)


def _stable(v):
    """A JSON-able, session-independent form of a settings value for the run key."""
    if isinstance(v, dict):
        return {str(k): _stable(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, (list, tuple)):
        return [_stable(x) for x in v]
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return float("{:.12g}".format(float(v)))
    if v is None:
        return None
    if isinstance(v, str):
        return v if " at 0x" not in v else "<object>"
    return "<{}>".format(type(v).__name__)


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def _write_json(path, obj):
    tmp = path + ".part"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, default=workflow._json_default)
    os.replace(tmp, path)


def _npy(work, tag, kind):
    return os.path.join(work, "{}_{}.npy".format(tag, kind))


def _load_done(work, tag, key, shape):
    """The stored row of a finished run with this key, or None."""
    meta = _read_json(os.path.join(work, tag + ".json"))
    if not meta or meta.get("key") != key or not isinstance(meta.get("row"), dict):
        return None
    for kind in KINDS:
        p = _npy(work, tag, kind)
        if not os.path.exists(p):
            return None
        try:
            mm = np.load(p, mmap_mode="r")
            ok = tuple(mm.shape) == tuple(shape) and mm.dtype == np.float32
            del mm
        except (OSError, ValueError):
            return None
        if not ok:
            return None
    return meta["row"]


def _save_run(work, tag, key, arrays, row, sample):
    """float32 .npy files (written through a memmap, then renamed into place), then
    the JSON that marks the run finished."""
    js = os.path.join(work, tag + ".json")
    for kind in KINDS:
        final = _npy(work, tag, kind)
        tmp = final + ".part"
        a = arrays[kind]
        mm = np.lib.format.open_memmap(tmp, mode="w+", dtype=np.float32, shape=a.shape)
        mm[:] = a
        mm.flush()
        del mm
        os.replace(tmp, final)
    _write_json(js, {"key": key, "sample": sample, "row": row})


def _method_params(dam, method, st):
    """Breach parameters of `method` for this dam with no user overrides (the values
    the width and time factors multiply when the method is drawn)."""
    ov = brmod.BreachOverrides(depth_fraction=getattr(st.overrides, "depth_fraction", None))
    if st.scenario == workflow.SCENARIO_FLOOD and st.flood is not None:
        from . import flood as floodmod
        fr = floodmod.route_flood(dam, method, ov, st.flood)
        if not fr.failed or fr.with_breach is None:
            return {}
        return dict(fr.with_breach.params)
    return dict(brmod.make_hydrograph(dam, method, ov).params)


def _settings_for(st, smp, params, plan, write_final):
    """Run settings of one sample: the sensitivity case settings (fixed plan,
    LISFLOOD-FP, Manning's n and the n raster scaled), with the width and time
    factors applied to the drawn method's own values."""
    m = smp["method"]
    p = params.get(m, {})
    case = sensmod.Case("run {}".format(smp["run"]), GROUP, width_factor=smp["width_factor"],
                        time_factor=smp["time_factor"], n_factor=smp["n_factor"])
    if m == st.breach_method:
        c = sensmod._case_settings(st, case, p, plan)
    else:       # a method case starts from no overrides and does not scale: scale it here
        c = sensmod._case_settings(st, dataclasses.replace(case, method=m), p, plan)
        c.breach_method = m
        w, tf = p.get("avg_breach_width_ft"), p.get("formation_time_hr")
        if w and case.width_factor != 1.0:
            c.overrides.avg_width_ft = w * case.width_factor
        if tf and case.time_factor != 1.0:
            c.overrides.formation_hr = tf * case.time_factor
    rise = p.get("rise_time_hr")
    if m in brmod.TR60_METHODS and rise and case.time_factor != 1.0:
        c.overrides.tr60_rise_hr = rise * case.time_factor
    c.resume = False                 # this module resumes whole runs itself
    c.keep_model_files = False       # N runs of model files add up; the arrays are kept
    c.baseline = False               # the no-breach flood does not depend on the breach
    c.write_final = bool(write_final)
    c.products = False
    return c


def _receptor_counts(rec, grid, arrays, arrival_m):
    if rec is None or not len(rec):
        return {}
    summ, _ = impmod.summarize(rec, grid, arrays, arrival_m)
    return {"structures_flooded": summ.get("structure", {}).get("flooded", 0),
            "road_km_flooded": summ.get("road", {}).get("flooded", 0.0),
            "rail_km_flooded": summ.get("rail", {}).get("flooded", 0.0),
            "structures_depth_ge_3ft": summ.get("structure", {}).get("depth_ge_3ft", 0)}


def _fmt(v, nd=4):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return ""
    if isinstance(v, float):
        return float("{:.{}g}".format(v, nd + 2))
    return v


def _pct(vals, q):
    return float(np.percentile(np.asarray(vals, dtype=float), q))


def _duration(s):
    if s < 60.0:
        return "under a minute"
    if s < 3600.0:
        return "{:.0f} min".format(s / 60.0)
    return "{:.1f} h".format(s / 3600.0)


def _drop_raw_results(folder):
    """LISFLOOD-FP's own result grids of a Monte Carlo run (a dozen text grids): the
    arrays are in _work already. The run report and QA rasters stay."""
    import shutil
    shutil.rmtree(os.path.join(folder, workflow.MODEL_FOLDER, "lisflood", "results"),
                  ignore_errors=True)


def _per_run_seconds(rep, wall_s):
    """Time of one run on the final domain: the base run's last LISFLOOD-FP run, or
    (when that rounds to 0 or is missing) the base run's wall time per model run."""
    runs = rep.get("lisflood_runs") or []
    last = runs[-1].get("runtime_min") if runs else None
    if last:
        return float(last) * 60.0
    extra = 1 if ("incremental" in rep or rep.get("baseline_runtime_min") is not None) else 0
    return wall_s / max(1, len(runs) + extra)


def _run_raster_paths(folder):
    return {k: os.path.join(folder, workflow.FINAL_NAMES[k]) for k in RUN_KINDS}


def run_rasters_current(folder, key):
    """Are the per-run rasters in `folder` those of the run with this key?"""
    meta = _read_json(os.path.join(folder, RUN_JSON))
    return bool(meta) and meta.get("key") == key and all(
        os.path.exists(p) for p in _run_raster_paths(folder).values())


def write_run_rasters(folder, grid, writer, arrays):
    """Depth (ft), velocity (ft/s) and arrival (hours) of one run from its SI arrays (m, m/s,
    hours; NaN = dry), on the base run's grid, with the study's raster writer."""
    os.makedirs(folder, exist_ok=True)
    out = {}
    for kind, p in _run_raster_paths(folder).items():
        a = np.asarray(arrays[kind], dtype=np.float64)
        writer(grid.like(workflow._to_us(kind, a), np.nan), p)
        out[kind] = p
    return out


def run_raster_bytes(shape):
    """Uncompressed size of one run's three float32 rasters (bytes)."""
    return 3 * 4 * int(shape[0]) * int(shape[1])


def _size_text(b):
    if b >= 1e9:
        return "{:.1f} GB".format(b / 1e9)
    return "{:.0f} MB".format(max(b / 1e6, 0.1)) if b >= 1e6 else "{:.1f} MB".format(b / 1e6)


def _baseline_refs(base, folder):
    """{kind: path relative to `folder`} of the base run's Baseline rasters, or None."""
    b = getattr(base, "baseline", None) or {}
    refs = {k: os.path.relpath(p, folder).replace(os.sep, "/") for k, p in b.items()
            if p and os.path.exists(p)}
    return refs or None


def _run_meta(out, dam, st, smp, row, key, weight, base_refs, grid):
    return {"format": RUN_FORMAT, "format_version": 1,
            "tool": "ND Dam Breach Runner " + workflow.VERSION,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "dam_id": dam.dam_id, "run": smp["run"], "key": key, "status": row.get("status"),
            "seed": out.seed, "n_runs": out.n, "weight": weight, "weight_note": WEIGHT_NOTE,
            "sample": {k: v for k, v in smp.items()},
            "parameters": {k: row.get(k) for k in ("avg_breach_width_ft", "formation_time_hr",
                                                  "tr60_rise_time_hr", "manning_n",
                                                  "peak_cfs")},
            "rasters": {k: workflow.FINAL_NAMES[k] for k in RUN_KINDS}, "units": RUN_UNITS,
            "arrival_depth_ft": st.arrival_depth_ft, "scenario": st.scenario,
            "baseline": base_refs,
            "baseline_note": ("the base run's no-breach rasters in the dam folder (paths "
                              "relative to this folder); not copied" if base_refs else
                              "no baseline: the scenario has no no-breach run"),
            "grid": {"x0": grid.x0, "y0": grid.y0, "cell": grid.dx, "nx": grid.nx,
                     "ny": grid.ny, "note": "the model grid; the rasters are written in the "
                                            "DEM's CRS like the percentile rasters"}}


def _finish_run_rasters(out, dam, st, grid, writer, samples, keys, ok_rows, log):
    """After the runs: every finished run has current rasters and a run.json with its final
    weight; other run folders lose theirs; Runs/index.json lists the runs."""
    by_run = {s["run"]: (s, k) for s, k in zip(samples, keys)}
    ok = [r for r in ok_rows if r["run"] in by_run]
    weight = 1.0 / len(ok) if ok else None
    refs_idx = _baseline_refs(out.base, out.runs_folder)
    written = 0
    keep = set()
    for row in ok:
        smp, key = by_run[row["run"]]
        tag = "run_{:03d}".format(row["run"])
        folder = os.path.join(out.runs_folder, tag)
        keep.add(tag)
        if not run_rasters_current(folder, key):
            arrays = {k: np.load(_npy(out.work_folder, tag, k), mmap_mode="r") for k in KINDS}
            write_run_rasters(folder, grid, writer, arrays)
            del arrays
            written += 1
        _write_json(os.path.join(folder, RUN_JSON), _run_meta(
            out, dam, st, smp, row, key, weight, _baseline_refs(out.base, folder), grid))
    removed = []
    try:
        names = sorted(os.listdir(out.runs_folder))
    except OSError:
        names = []
    for name in names:
        folder = os.path.join(out.runs_folder, name)
        if name in keep or not re.match(r"^run_\d+$", name) or not os.path.isdir(folder):
            continue
        if not os.path.exists(os.path.join(folder, RUN_JSON)):
            continue
        for p in list(_run_raster_paths(folder).values()) + [os.path.join(folder, RUN_JSON)]:
            try:
                os.remove(p)
            except OSError:
                pass
        removed.append(name)
    index = {"format": RUNS_INDEX_FORMAT, "format_version": 1,
             "tool": "ND Dam Breach Runner " + workflow.VERSION,
             "created": datetime.datetime.now().isoformat(timespec="seconds"),
             "dam_id": dam.dam_id, "seed": out.seed, "n": out.n, "scenario": st.scenario,
             "runs": [{"run": r["run"], "folder": "run_{:03d}".format(r["run"]),
                       "status": r.get("status"), "weight": weight} for r in ok],
             "weight_note": WEIGHT_NOTE, "rasters": {k: workflow.FINAL_NAMES[k]
                                                     for k in RUN_KINDS},
             "units": RUN_UNITS, "baseline": refs_idx,
             "note": "ND Dam Hazard's class probability tool classifies each run folder "
                     "(DAM_FOLDER = Runs/run_NNN); the baseline, when there is one, is the "
                     "base run's (paths relative to this folder)."}
    out.runs_index = os.path.join(out.runs_folder, RUNS_INDEX)
    _write_json(out.runs_index, index)
    msg = "Per-run rasters for the class probability: {} run folder(s) in {}".format(
        len(ok), out.runs_folder)
    if written:
        msg += " ({} written now)".format(written)
    if removed:
        msg += "; run.json and rasters removed from {} folder(s) no longer in the study ({})" \
            .format(len(removed), ", ".join(removed))
    log(msg + ".")
    out.run_folders = [os.path.join(out.runs_folder, "run_{:03d}".format(r["run"]))
                       for r in ok]


def run_probabilistic(dam, dam_xy, st, terrain_src, writer, out_folder, inflow_xy=None,
                      spec=None, n=DEFAULT_N, seed=DEFAULT_SEED,
                      thresholds_ft=DEFAULT_THRESHOLDS_FT, progress=None, log=None,
                      cancel=None, classify=None, run_rasters=False):
    """Monte Carlo breach runs on the base run's domain. `spec` is a ProbSpec (or its
    dict); `seed` None picks one (recorded). `classify(folder) -> str` (optional)
    classifies each run from the rasters in its folder (see
    handoff.make_classifier). Outputs go to <out_folder>/Probabilistic/.
    run_rasters (v3.2.0): also write each finished run's depth, velocity and arrival
    rasters and run.json into Probabilistic/Runs/run_NNN/ for ND Dam Hazard's class
    probability (see the module docstring)."""
    progress = progress or (lambda p, m="": None)
    log = log or (lambda m: None)
    cancel = cancel or (lambda: False)
    t_study = time.time()
    if spec is None:
        spec = ProbSpec()
    elif isinstance(spec, dict):
        spec = ProbSpec.from_dict(spec)
    spec = copy.deepcopy(spec).validate()
    thresholds_ft = _thresholds(thresholds_ft)
    out = ProbabilisticResult()
    out.spec = spec
    out.folder = os.path.join(out_folder, FOLDER)
    out.work_folder = os.path.join(out.folder, WORK_FOLDER)
    out.runs_folder = os.path.join(out.folder, RUNS_FOLDER)
    for d in (out.folder, out.work_folder, out.runs_folder):
        os.makedirs(d, exist_ok=True)
    n_req = int(n)
    n = max(MIN_RUNS, min(MAX_RUNS, n_req))
    if n != n_req:
        out.warnings.append("{} runs asked for; {} are run (between {} and {}).".format(
            n_req, n, MIN_RUNS, MAX_RUNS))
    if seed is None:
        seed = int(np.random.SeedSequence().entropy % (2 ** 31))
        log("No seed given: using seed {}. Give it again to repeat or continue this study."
            .format(seed))
    out.seed, out.n = int(seed), n

    # --- the base run: its plan (domain, inflow point, duration) is every run's -----------
    base_st = copy.copy(st)
    base_st.keep_arrays = False
    if st.tier != workflow.TIER_LISFLOOD:
        out.warnings.append("The probabilistic runs use LISFLOOD-FP only; the base run was "
                            "switched to LISFLOOD-FP too so that it is comparable.")
        base_st.tier = workflow.TIER_LISFLOOD
    if st.keep_model_files:
        base_st.resume = True         # a finished base run with identical settings is reused
    progress(0, "base run")
    t0 = time.time()
    base = workflow.run_dam(dam, dam_xy, base_st, terrain_src, writer, out_folder, inflow_xy,
                            workflow._span(progress, 0, 15), log, cancel)
    base_wall = time.time() - t0
    out.base = base
    if base.plan is None or base.grid is None:
        out.warnings.append("The dam does not fail in the base scenario; no probabilistic runs.")
        return out
    plan = dict(base.plan)
    grid = base.grid
    shape = (grid.ny, grid.nx)
    rep = _read_json(base.report_json)
    base_params = rep.get("hydrograph", {})
    params = {st.breach_method: base_params}

    # --- which methods can be drawn -------------------------------------------------------
    if spec.methods:
        keep = []
        for m, w in spec.methods:
            if m != st.breach_method:
                try:
                    params[m] = _method_params(dam, m, st)
                except brmod.BreachInputError as e:
                    out.warnings.append("Breach method {} left out of the draw: {}".format(m, e))
                    continue
            keep.append((m, w))
        if not keep:
            out.warnings.append("None of the listed breach methods can be used; every run uses "
                                "the base run's method.")
        spec.methods = keep
    for m, _p in spec.method_choices(st.breach_method):
        p = params.get(m, {})
        has_w, has_t = bool(p.get("avg_breach_width_ft")), bool(
            p.get("formation_time_hr") or p.get("rise_time_hr"))
        if spec.width.varies() and not has_w:
            out.warnings.append("{}: this method has no breach width, so the width factor does "
                                "not apply to runs that use it.".format(m))
        if spec.time.varies() and not has_t:
            out.warnings.append("{}: this method has no formation time, so the time factor does "
                                "not apply to runs that use it.".format(m))
    if any(m in brmod.TR60_METHODS for m, _p in spec.method_choices(st.breach_method)) and \
            spec.time.varies():
        out.warnings.append("TR-60 runs: the formation time factor scales the TR-60 rise time.")
    samples = draw_samples(spec, n, seed, st.breach_method)
    out.samples = samples

    # --- run keys: the sample and everything the arrays depend on --------------------------
    settings = {k: _stable(v) for k, v in workflow.key_settings(st).items()
                if k not in _KEY_SKIP}
    common = {"dam": _stable(brmod.dam_to_dict(dam)), "dam_xy": _stable(list(dam_xy)),
              "settings": settings, "shape": list(shape),
              "plan": _stable({"bounds": plan["bounds"], "sim_time": plan["sim_time"],
                               "inflow_xy": plan["inflow_xy"]}),
              "base": _stable({"peak_breach_cfs": base.summary.get("peak_breach_cfs"),
                               "flooded_acres": round(float(base.summary.get(
                                   "flooded_acres") or 0.0)),
                               "cell": grid.dx, "x0": grid.x0, "y0": grid.y0}),
              "params": _stable({m: {k: p.get(k) for k in ("avg_breach_width_ft",
                                                           "formation_time_hr", "rise_time_hr")}
                                 for m, p in params.items()})}

    def key_of(smp):
        return workflow._hash(common, _stable({k: v for k, v in smp.items() if k != "u"}))

    keys = [key_of(s) for s in samples]
    done = {}
    for smp, key in zip(samples, keys):
        tag = "run_{:03d}".format(smp["run"])
        row = _load_done(out.work_folder, tag, key, shape)
        if row is not None:
            done[smp["run"]] = row
    per_run_s = _per_run_seconds(rep, base_wall)
    todo = n - len(done)
    out.estimate_min = round(todo * per_run_s / 60.0, 1)
    msg = "Probabilistic study: {} runs, seed {}, Latin hypercube sampling".format(n, seed)
    if done:
        msg += "; {} already finished for the same samples (resumed)".format(len(done))
    if todo:
        msg += (". One LISFLOOD-FP run on the base run's domain took {}, so the {} run{} to do "
                "should take about {} (an estimate: a lower Manning's n or a wider, faster "
                "breach runs slower).".format(_duration(per_run_s), todo,
                                              "" if todo == 1 else "s",
                                              _duration(todo * per_run_s)))
    else:
        msg += "; nothing left to run."
    log(msg)
    if run_rasters:
        per = run_raster_bytes(shape)
        log("Per-run rasters for the class probability are on: about {} per run (three "
            "float32 rasters of {:,} cells before GeoTIFF compression, which shrinks mostly "
            "dry rasters a lot), up to about {} for the {} runs, in {}. The _work arrays "
            "take about as much again; delete _work when the study is final.".format(
                _size_text(per), shape[0] * shape[1], _size_text(per * n), n,
                out.runs_folder))

    # --- the runs -------------------------------------------------------------------------
    arrival_m = st.arrival_depth_ft * FT
    run_times = []
    rows = []
    for i, smp in enumerate(samples):
        if cancel():
            raise lfmod.Cancelled("cancelled")
        tag = "run_{:03d}".format(smp["run"])
        if smp["run"] in done:
            row = dict(done[smp["run"]])
            row["resumed"] = True
            rows.append(row)
            out.resumed += 1
            continue
        log("=== probabilistic run {}/{}: width x{:.3f}, time x{:.3f}, Manning's n x{:.3f}, {}"
            .format(smp["run"], n, smp["width_factor"], smp["time_factor"], smp["n_factor"],
                    smp["method"]))
        try:
            os.remove(os.path.join(out.work_folder, tag + ".json"))
        except OSError:
            pass
        folder = os.path.join(out.runs_folder, tag)
        row = {"run": smp["run"], "resumed": False, "method": smp["method"],
               "width_factor": smp["width_factor"], "time_factor": smp["time_factor"],
               "n_factor": smp["n_factor"], "manning_n": st.manning_n * smp["n_factor"]}
        t1 = time.time()
        try:
            cst = _settings_for(st, smp, params, plan, classify is not None)
            r = workflow.run_dam(dam, dam_xy, cst, terrain_src, writer, folder, inflow_xy,
                                 workflow._span(progress, 15 + 80.0 * i / n,
                                                15 + 80.0 * (i + 1) / n), log, cancel)
        except (brmod.BreachInputError, lfmod.ModelError) as e:
            row["status"] = "failed: {}".format(str(e)[:200])
            out.warnings.append("Run {}: {}".format(smp["run"], e))
            rows.append(row)
            continue
        if r.final_si is None:
            arrays = {k: np.full(shape, np.nan, np.float32) for k in KINDS}
            row["status"] = "no breach"
        else:
            arrays = {k: np.asarray(r.final_si[k], dtype=np.float32) for k in KINDS}
            if any(a.shape != shape for a in arrays.values()):
                row["status"] = "failed: grid {} differs from the base run's {}".format(
                    arrays["depth"].shape, shape)
                out.warnings.append("Run {}: {}".format(smp["run"], row["status"][8:]))
                rows.append(row)
                continue
            row["status"] = "done"
        s = r.summary or {}
        hp = _read_json(r.report_json).get("hydrograph", {})
        row.update({"peak_cfs": s.get("peak_breach_cfs"), "flooded_ac": s.get("flooded_acres", 0),
                    "flooded_ac_over_arrival_depth": s.get("flooded_acres_over_arrival_depth", 0),
                    "max_depth_ft": s.get("max_depth_ft", 0),
                    "max_velocity_fps": s.get("max_velocity_fps", 0),
                    "latest_arrival_hr": s.get("latest_arrival_hr"),
                    "avg_breach_width_ft": hp.get("avg_breach_width_ft"),
                    "formation_time_hr": hp.get("formation_time_hr"),
                    "tr60_rise_time_hr": hp.get("rise_time_hr")})
        if any("leaves the base domain" in w for w in (r.warnings or [])):
            row["domain_too_small"] = True
        if classify is not None and row["status"] == "done":
            try:
                row["hazard_class"] = classify(folder)
            except Exception as e:           # the classification is an extra
                row["hazard_class"] = "error: {}".format(str(e)[:80])
        dt = time.time() - t1
        row["minutes"] = round(dt / 60.0, 2)
        _save_run(out.work_folder, tag, keys[i], arrays, row,
                  {k: v for k, v in smp.items()})
        if run_rasters:          # written as each run finishes (an interrupted study keeps them)
            write_run_rasters(folder, grid, writer, arrays)
            _write_json(os.path.join(folder, RUN_JSON), _run_meta(
                out, dam, st, smp, row, keys[i], 1.0 / n, _baseline_refs(out.base, folder),
                grid))
        del arrays, r
        _drop_raw_results(folder)
        rows.append(row)
        run_times.append(dt)
        left = sum(1 for s2 in samples[i + 1:] if s2["run"] not in done)
        log("Run {}/{} finished in {}; about {} left.".format(
            smp["run"], n, _duration(dt), _duration(left * float(np.mean(run_times)))))
    out.rows = rows

    # --- statistics -----------------------------------------------------------------------
    progress(95, "statistics over the runs")
    ok_rows = [r for r in rows if r.get("status") in _OK]
    out.completed = len(ok_rows)
    failed = [r for r in rows if r.get("status") not in _OK]
    if failed:
        out.warnings.append("{} of {} runs failed and are left out of the statistics (runs {})."
                            " Leaving runs out can bias the result; check them and run the "
                            "study again (finished runs are kept).".format(
                                len(failed), n, ", ".join(str(r["run"]) for r in failed)))
    if run_rasters:
        _finish_run_rasters(out, dam, st, grid, writer, samples, keys, ok_rows, log)
    if not ok_rows:
        out.warnings.append("No run finished; nothing to summarise.")
        write_outputs(out, st, dam, grid, None, thresholds_ft, rep, plan, t_study)
        return out
    tags = ["run_{:03d}".format(r["run"]) for r in ok_rows]
    stacks = {k: [np.load(_npy(out.work_folder, t, k), mmap_mode="r") for t in tags]
              for k in KINDS}
    stats = blockwise_stats(stacks["depth"], stacks["velocity"], stacks["arrival"], shape,
                            [t * FT for t in thresholds_ft])
    # per-run receptor counts from the stored arrays (so resumed runs count the same way)
    if st.receptors is not None and len(st.receptors):
        for j, row in enumerate(ok_rows):
            row.update(_receptor_counts(st.receptors, grid, {
                "depth": stacks["depth"][j], "velocity": stacks["velocity"][j],
                "arrival": stacks["arrival"][j]}, arrival_m))
    stacks = None                    # release the memory maps
    if classify is not None:
        for row in ok_rows:
            if row.get("resumed") and not row.get("hazard_class"):
                f = os.path.join(out.runs_folder, "run_{:03d}".format(row["run"]))
                old_f = os.path.join(out.folder, LEGACY_RUNS_FOLDER, os.path.basename(f))
                if not os.path.exists(os.path.join(f, workflow.FINAL_NAMES["depth"])) and \
                        os.path.exists(os.path.join(old_f, workflow.FINAL_NAMES["depth"])):
                    f = old_f            # a study begun with 3.1.x on a case-sensitive disk
                if os.path.exists(os.path.join(f, workflow.FINAL_NAMES["depth"])):
                    try:
                        row["hazard_class"] = classify(f)
                    except Exception as e:
                        row["hazard_class"] = "error: {}".format(str(e)[:80])
                else:
                    row["hazard_class"] = "not classified (run finished without classification)"

    # rasters, US units
    names = raster_names(thresholds_ft)
    for i, t in enumerate(thresholds_ft):
        p = stats["prob"][i]
        _put(out, writer, grid, names[("prob", t)], np.where(p > 0, p, np.nan))
    for q in DEPTH_Q:
        a = stats["depth"][q]
        _put(out, writer, grid, names[("depth", q)], np.where(a > 1e-4, a / FT, np.nan))
    v, dp = stats["velocity"], stats["depth"][DEPTH_Q[-1]]
    _put(out, writer, grid, names[("velocity", VELOCITY_Q)],
         np.where((v > 1e-6) | (dp > 1e-4), v / FT, np.nan))
    _put(out, writer, grid, names[("arrival", ARRIVAL_Q)], stats["arrival"])
    write_outputs(out, st, dam, grid, stats, thresholds_ft, rep, plan, t_study)
    progress(100, "done")
    return out


def _put(out, writer, grid, name, a):
    p = os.path.join(out.folder, name)
    writer(grid.like(np.asarray(a, dtype=np.float64), np.nan), p)
    out.rasters[name] = p


# ----------------------------------------------------------------------------------------
# tables, plot, report, manifest
# ----------------------------------------------------------------------------------------
def _base_values(base, rep):
    s = (base.summary if base is not None else None) or {}
    h = rep.get("hydrograph", {})
    b = {"peak_cfs": s.get("peak_breach_cfs"), "flooded_ac": s.get("flooded_acres"),
         "flooded_ac_over_arrival_depth": s.get("flooded_acres_over_arrival_depth"),
         "max_depth_ft": s.get("max_depth_ft"), "max_velocity_fps": s.get("max_velocity_fps"),
         "latest_arrival_hr": s.get("latest_arrival_hr"),
         "avg_breach_width_ft": h.get("avg_breach_width_ft"),
         "formation_time_hr": h.get("formation_time_hr"), "width_factor": 1.0,
         "time_factor": 1.0, "n_factor": 1.0}
    r = rep.get("receptors")
    if isinstance(r, dict):
        b.update({"structures_flooded": r.get("structure", {}).get("flooded", 0),
                  "structures_depth_ge_3ft": r.get("structure", {}).get("depth_ge_3ft", 0),
                  "road_km_flooded": r.get("road", {}).get("flooded", 0.0),
                  "rail_km_flooded": r.get("rail", {}).get("flooded", 0.0)})
    return b


def summary_rows(ok_rows, base_vals):
    out = []
    for key, unit in _METRICS:
        vals = [float(r[key]) for r in ok_rows if isinstance(r.get(key), (int, float))
                and not isinstance(r.get(key), bool) and math.isfinite(float(r[key]))]
        if not vals:
            continue
        a = np.asarray(vals)
        out.append({"metric": key, "unit": unit, "runs": len(vals), "mean": _fmt(a.mean()),
                    "std": _fmt(float(a.std(ddof=1)) if len(a) > 1 else 0.0),
                    "min": _fmt(a.min()), "p10": _fmt(_pct(a, 10)), "p50": _fmt(_pct(a, 50)),
                    "p90": _fmt(_pct(a, 90)), "max": _fmt(a.max()),
                    "base": _fmt(base_vals.get(key))})
    return out


def _running(vals):
    xs, mean, p90 = [], [], []
    for k in range(1, len(vals) + 1):
        xs.append(k)
        mean.append(float(np.mean(vals[:k])))
        p90.append(_pct(vals[:k], 90))
    return np.asarray(xs, float), np.asarray(mean), np.asarray(p90)


def write_outputs(out, st, dam, grid, stats, thresholds_ft, rep, plan, t_study):
    ok_rows = [r for r in out.rows if r.get("status") in _OK]
    base_vals = _base_values(out.base, rep)
    # per-run table
    out.runs_csv = os.path.join(out.folder, "Probabilistic_Runs.csv")
    with open(out.runs_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RUN_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in out.rows:
            w.writerow({k: _fmt(v, 6) if isinstance(v, float) else v for k, v in r.items()})
    # summary table
    srows = summary_rows(ok_rows, base_vals)
    extra = []
    cell_ac = grid.dx ** 2 / ACRE if grid is not None else 0.0
    area = {}
    if stats is not None:
        for i, t in enumerate(thresholds_ft):
            for p in (0.1, 0.5, 0.9):
                ac = float((stats["prob"][i] >= p - 1e-9).sum()) * cell_ac
                area[(t, p)] = ac
                extra.append({"metric": "area with P(depth >= {:g} ft) >= {:g}".format(t, p),
                              "unit": "ac", "runs": len(ok_rows), "mean": round(ac, 1)})
    sf = [r.get("structures_flooded") for r in ok_rows if r.get("structures_flooded") is not None]
    if sf:
        extra.append({"metric": "any structure flooded", "unit": "share of runs",
                      "runs": len(sf), "mean": round(float(np.mean([x > 0 for x in sf])), 4)})
    cls = [r.get("hazard_class") for r in ok_rows if r.get("hazard_class") and not str(
        r.get("hazard_class")).startswith(("error:", "not classified"))]
    for c in sorted(set(cls)):
        extra.append({"metric": "hazard class = {}".format(c), "unit": "share of runs",
                      "runs": len(cls), "mean": round(cls.count(c) / float(len(cls)), 4)})
    out.summary_csv = os.path.join(out.folder, "Probabilistic_Summary.csv")
    with open(out.summary_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(srows + extra)
    # receptors: each structure's probabilities (the rasters sampled at the structure)
    rec_rows = []
    rec = st.receptors
    if stats is not None and rec is not None and len(rec):
        m = (rec.kind == impmod.KIND_STRUCTURE) & rec.in_bounds(grid.bounds)
        if m.any():
            sub = rec.subset(m)
            probs = [sub.sample(grid, p) for p in stats["prob"]]
            d50 = sub.sample(grid, stats["depth"][50.0])
            d90 = sub.sample(grid, stats["depth"][DEPTH_Q[-1]])
            a10 = sub.sample(grid, stats["arrival"])
            for j in range(len(sub)):
                row = {"id": sub.ident[j], "x": round(float(sub.x[j]), 1),
                       "y": round(float(sub.y[j]), 1)}
                for i, t in enumerate(thresholds_ft):
                    row["p_depth_ge_{:g}ft".format(t)] = round(float(np.nan_to_num(
                        probs[i][j])), 4)
                row["depth_p50_ft"] = round(float(d50[j]) / FT, 2) if d50[j] > 1e-4 else 0.0
                row["depth_p90_ft"] = round(float(d90[j]) / FT, 2) if d90[j] > 1e-4 else 0.0
                row["arrival_p10_hr"] = round(float(a10[j]), 2) if np.isfinite(a10[j]) else ""
                rec_rows.append(row)
            k0 = "p_depth_ge_{:g}ft".format(thresholds_ft[0])
            rec_rows.sort(key=lambda r: (-r[k0], str(r["id"])))
            out.receptors_csv = os.path.join(out.folder, "Probabilistic_Receptors.csv")
            fields = (["id", "x", "y"] + ["p_depth_ge_{:g}ft".format(t) for t in thresholds_ft]
                      + ["depth_p50_ft", "depth_p90_ft", "arrival_p10_hr"])
            with open(out.receptors_csv, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(rec_rows)
    # convergence plot
    fl = [float(r["flooded_ac"]) for r in ok_rows if isinstance(r.get("flooded_ac"), (int, float))]
    conv_note = ""
    if len(fl) >= 2:
        from . import products
        xs, mean, p90 = _running(fl)
        svg = products.line_plot_svg(
            [("Running mean", xs, mean, "#1f77b4"), ("Running P90", xs, p90, "#d62728")],
            "Flooded area as runs are added", xlabel="Number of runs",
            ylabel="Flooded area (ac)")
        out.convergence_svg = os.path.join(out.folder, "Probabilistic_Convergence.svg")
        with open(out.convergence_svg, "w", encoding="utf-8") as f:
            f.write(svg)
        if len(fl) >= 10:
            h = len(fl) // 2
            ch_m = abs(mean[-1] - mean[h - 1]) / max(abs(mean[-1]), 1e-9)
            ch_p = abs(p90[-1] - p90[h - 1]) / max(abs(p90[-1]), 1e-9)
            conv_note = ("Over the second half of the runs the mean flooded area changed by "
                         "{:.1%} and its P90 by {:.1%}.".format(ch_m, ch_p))
            if max(ch_m, ch_p) > 0.05:
                conv_note += " That is more than 5%: more runs would firm up the estimates."
    # flags
    flags = []
    if ok_rows:
        def spread(key, label, unit, nd=0):
            v = [float(r[key]) for r in ok_rows if isinstance(r.get(key), (int, float))
                 and not isinstance(r.get(key), bool)]
            if not v:
                return
            b = base_vals.get(key)
            flags.append("{}: P10 {:,.{nd}f}, median {:,.{nd}f}, P90 {:,.{nd}f} {} over {} runs{}."
                         .format(label, _pct(v, 10), _pct(v, 50), _pct(v, 90), unit, len(v),
                                 " (base run {:,.{nd}f})".format(b, nd=nd)
                                 if isinstance(b, (int, float)) else "", nd=nd))
        spread("peak_cfs", "Peak breach outflow", "cfs")
        spread("flooded_ac_over_arrival_depth",
               "Flooded area deeper than {:g} ft".format(st.arrival_depth_ft), "ac")
        spread("max_depth_ft", "Deepest water", "ft", 1)
        for t in thresholds_ft:
            if (t, 0.5) in area:
                flags.append("Area with at least a 50% chance of depth {:g} ft or more: {:,.0f} "
                             "ac (10% chance: {:,.0f} ac; 90% chance: {:,.0f} ac).".format(
                                 t, area[(t, 0.5)], area[(t, 0.1)], area[(t, 0.9)]))
        if sf:
            p_any = float(np.mean([x > 0 for x in sf]))
            flags.append("Structures flooded (depth {:g} ft or more): {} to {} across the runs; "
                         "probability that at least one is: {:.0%}.".format(
                             st.arrival_depth_ft, min(sf), max(sf), p_any))
            if 0.0 < p_any < 1.0:
                flags.append("CHANGES THE ANSWER: some runs flood no structure and others do; "
                             "the breach and roughness uncertainty decides whether people are at "
                             "risk. Review the inputs and the hazard classification.")
            if rec_rows:
                k0 = "p_depth_ge_{:g}ft".format(thresholds_ft[0])
                flags.append("Structures with at least a 50% chance of depth {:g} ft or more: "
                             "{} of {} in the model area.".format(
                                 thresholds_ft[0], sum(1 for r in rec_rows if r[k0] >= 0.5),
                                 len(rec_rows)))
        if cls:
            uniq = sorted(set(cls))
            if len(uniq) > 1:
                flags.append("CHANGES THE HAZARD CLASS: " + ", ".join(
                    "{} in {:.0%} of the runs".format(c, cls.count(c) / float(len(cls)))
                    for c in uniq) + ".")
            else:
                flags.append("The hazard class is {} in every run.".format(uniq[0]))
        small = [r["run"] for r in ok_rows if r.get("domain_too_small")]
        if small:
            out.warnings.append("{} run(s) leave the base domain deeper than the edge tolerance "
                                "(runs {}); their flood is cut off at the edge, so the "
                                "probabilities near the edges are too low. Enlarge the base "
                                "domain (longer first domain or a larger extension) and run the "
                                "study again.".format(len(small), ", ".join(map(str, small))))
        if conv_note:
            flags.append(conv_note)
    out.flags = flags
    _write_report(out, st, dam, grid, thresholds_ft, plan, srows)
    _write_manifest(out, st, dam, thresholds_ft, rep, plan, t_study)


def _write_report(out, st, dam, grid, thresholds_ft, plan, srows):
    from . import products
    ok = [r for r in out.rows if r.get("status") in _OK]
    title = "Probabilistic breach runs: {}".format(dam.dam_id)
    md = ["# " + title, "",
          "{} Monte Carlo runs (Latin hypercube, seed {}), {} finished and counted{}. Every run "
          "uses the base run's inflow point, domain and simulation time; LISFLOOD-FP only."
          .format(out.n, out.seed, len(ok), "; {} reused from an earlier, interrupted study"
                  .format(out.resumed) if out.resumed else ""), ""]
    if plan:
        b = plan["bounds"]
        md += ["Domain {:.1f} x {:.1f} mi at {:g} m cells, {:.1f} h simulated.".format(
            (b[2] - b[0]) / 1609.344, (b[3] - b[1]) / 1609.344, grid.dx,
            plan["sim_time"] / 3600.0), ""]
    md += ["## What the spread means", ""] + ["- " + x for x in out.flags] + [""]
    if out.warnings:
        md += ["## Check these", ""] + ["- " + x for x in out.warnings] + [""]
    md += ["## Uncertain inputs", "", "| Input | Distribution (factor on the base value) |",
           "|---|---|"]
    md += ["| {} | {} |".format(a, b) for a, b in out.spec.describe()]
    md += ["", "- " + DEFAULTS_NOTE, "- " + POOL_NOTE,
           "- Manning's n factors multiply the single n and the roughness raster alike. The "
           "width and time factors multiply the drawn method's own breach width and formation "
           "time (the TR-60 rise time for TR-60).", ""]
    if srows:
        md += ["## Results over the runs", "",
               "| Metric | Unit | Runs | Mean | P10 | P50 | P90 | Base |", "|---|---|---|---|---|"
               "---|---|---|"]
        md += ["| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            r["metric"].replace("_", " "), r["unit"], r["runs"], r["mean"], r["p10"], r["p50"],
            r["p90"], r["base"]) for r in srows]
        md.append("")
    md += ["## Rasters", "",
           "Probability = share of the finished runs with at least that depth (blank where no "
           "run reaches it). Depth and velocity percentiles count a dry run as 0. The arrival "
           "P10 is the 10th percentile over the runs that reach the arrival depth ({:g} ft) in "
           "the cell, blank where fewer than {:.0%} of the runs reach it; it is an early, "
           "plausible arrival, not the earliest possible one.".format(
               st.arrival_depth_ft, MIN_REACHED), ""]
    md += ["- {}".format(p) for p in out.rasters.values()]
    md += ["", "## Runs", "", "| Run | Method | Width x | Time x | n x | Peak cfs | Flooded ac | "
           "Max depth ft | Structures | Class | Status |", "|---|---|---|---|---|---|---|---|---|"
           "---|---|"]
    for r in out.rows:
        md.append("| {} | {} | {:.3f} | {:.3f} | {:.3f} | {} | {} | {} | {} | {} | {}{} |".format(
            r["run"], r.get("method", ""), r.get("width_factor", 0), r.get("time_factor", 0),
            r.get("n_factor", 0), r.get("peak_cfs", ""), r.get("flooded_ac", ""),
            r.get("max_depth_ft", ""), r.get("structures_flooded", ""),
            r.get("hazard_class", ""), r.get("status", ""), " (resumed)" if r.get("resumed")
            else ""))
    md += ["", "## Files", "", "- Per-run table: {}".format(out.runs_csv),
           "- Summary: {}".format(out.summary_csv)]
    if out.receptors_csv:
        md.append("- Structure probabilities: {}".format(out.receptors_csv))
    md += ["- Seed, distributions and every sample: {}".format(
        os.path.join(out.folder, "probabilistic_manifest.json")),
        "- Per-run arrays (float32 .npy, SI units; they let an interrupted study continue, "
        "delete the folder when the study is final): {}".format(out.work_folder),
        "- Per-run folders (run report and QA rasters; LISFLOOD-FP's own result grids are not "
        "kept): {}".format(out.runs_folder)]
    if out.runs_index:
        md.append("- Per-run rasters for ND Dam Hazard's class probability (Depth (Max).tif ft, "
                  "Velocity (Max).tif ft/s, Arrival Time.tif hours, run.json with the sample "
                  "and its weight; the baseline is referenced, not copied): {} run folders, "
                  "listed in {}".format(len(out.run_folders), out.runs_index))
    md.append("")
    extra = ""
    if out.convergence_svg:
        md += ["## Convergence", "",
               "Running mean and P90 of the flooded area as runs are added, in run order. With "
               "Latin hypercube sampling only the full set is stratified, so the curve before "
               "the last run is indicative.", ""]
        with open(out.convergence_svg, encoding="utf-8") as f:
            extra = f.read()
    text = "\n".join(md)
    out.report_md = os.path.join(out.folder, "Probabilistic_Report.md")
    with open(out.report_md, "w", encoding="utf-8") as f:
        f.write(text)
    out.report_html = os.path.join(out.folder, "Probabilistic_Report.html")
    with open(out.report_html, "w", encoding="utf-8") as f:
        f.write(products.markdown_to_html(text, title, extra))


def _write_manifest(out, st, dam, thresholds_ft, rep, plan, t_study):
    out.manifest = os.path.join(out.folder, "probabilistic_manifest.json")
    m = {"tool": "ND Dam Breach Runner " + workflow.VERSION,
         "created": datetime.datetime.now().isoformat(timespec="seconds"),
         "dam_id": dam.dam_id, "seed": out.seed, "n": out.n,
         "random_generator": "numpy.random.default_rng (PCG64), numpy {}".format(np.__version__),
         "sampling": "Latin hypercube over 4 dimensions in this order: breach width, formation "
                     "time, Manning's n, breach method. For each dimension: u = (permutation of "
                     "0..N-1 + uniform[0,1)) / N, then the inverse CDF of the distribution "
                     "(lognormal truncated at the {:g} and {:g} quantiles); the method is picked "
                     "from the cumulative weights.".format(LOGN_TAIL, 1 - LOGN_TAIL),
         "spec": out.spec.to_dict(), "defaults_note": DEFAULTS_NOTE, "pool_level": POOL_NOTE,
         "thresholds_ft": list(thresholds_ft), "arrival_depth_ft": st.arrival_depth_ft,
         "base_method": st.breach_method, "base_hydrograph": rep.get("hydrograph", {}),
         "base_manning_n": st.manning_n,
         "plan": None if plan is None else {"bounds": plan["bounds"],
                                            "sim_time_hr": plan["sim_time"] / 3600.0,
                                            "inflow_xy": plan["inflow_xy"]},
         "estimate_min": out.estimate_min,
         "elapsed_min": round((time.time() - t_study) / 60.0, 1),
         "samples": out.samples,
         "runs": [{"run": r["run"], "status": r.get("status"), "resumed": r.get("resumed")}
                  for r in out.rows],
         "outputs": {"rasters": out.rasters, "runs_csv": out.runs_csv,
                     "summary_csv": out.summary_csv, "receptors_csv": out.receptors_csv,
                     "convergence_svg": out.convergence_svg, "report_html": out.report_html,
                     "report_md": out.report_md, "work_folder": out.work_folder,
                     "runs_folder": out.runs_folder, "runs_index": out.runs_index,
                     "run_folders": out.run_folders},
         "flags": out.flags, "warnings": out.warnings}
    _write_json(out.manifest, m)
