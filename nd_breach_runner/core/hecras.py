"""HEC-RAS interchange.

EXPORT (this plugin -> HEC-RAS)

* `write_hydrograph_files`: the breach hydrograph as an unsteady-flow text block in the
  HEC-RAS `.u##` convention ("Flow Hydrograph= N", then 10 values per line in 8-character
  fields) and as a plain CSV (time hr, cfs), on a regular time step (default 5 min). The
  peak is kept exactly (see `resample_hydrograph`).
* `write_breach_table`: the values HEC-RAS's breach editor asks for, from the DamData and the
  parameters dict of the breach module, with the plugin's estimates marked.

IMPORT (HEC-RAS 2D plan HDF -> rasters)

* `read_mesh` reads the maximum results of one 2D flow area from a `.p##.hdf`, `to_grids`
  turns them into regular north-up `Grid`s, `import_as_reference` samples them straight onto a
  Grid (the plugin's own, say) as arrays for `benchmark.compare_fields`.

Only numpy and the standard library are used. The HDF file is read with h5py, which is
optional: without it the reader stops with a message saying how to install it. h5py is not
part of QGIS; install it into QGIS's own Python.

HDF layout assumed (RAS 5.0.x to 6.x; layouts vary, so datasets are found by walking the file
and matching names case-insensitively, see `discover`). Paths are examples only:

    Geometry/2D Flow Areas/<area>/Cells Center Coordinate      (N, 2) x, y in the CRS units
    Geometry/2D Flow Areas/<area>/Cells Minimum Elevation      (N,)
    Geometry/2D Flow Areas/<area>/Faces Cell Indexes           (F, 2) cells either side of a face
    Results/Unsteady/Output/Output Blocks/Base Output/Summary Output/2D Flow Areas/<area>/
        Maximum Water Surface                                  (2, N) row 0 value, row 1 time (days)
        Maximum Face Velocity                                  (2, F) row 0 value, row 1 time
        (a per-cell "Maximum ... Velocity" dataset is used instead when there is one)
    Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/
        Time                                                   (nt,) days since the start
        2D Flow Areas/<area>/Water Surface                     (nt, N) for the arrival time

The result datasets can have fewer cells than the geometry (RAS adds ghost cells beyond the
real ones): the first N cells are used, N being the length of Maximum Water Surface.
Units come from the 'Units' attribute on the dataset, its groups or the file root ('Units
System'); the default, when none is found, is US customary. Everything returned is US ft,
ft/s and hours.
"""
import csv
import math
import os
import re

import numpy as np

from .grid import Grid
from .units import ACFT, CFS, FT

RAS_INTERVALS_MIN = (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60, 120, 180, 240, 360, 480, 720,
                     1440)
DEFAULT_STEP_MIN = 5.0
MODEL_FOLDER = "_breach_model"            # where workflow.py writes the run's files
HYDROGRAPH_CSV = "breach_hydrograph.csv"
RUN_REPORT_JSON = "run_report.json"

BASIS_EST = "Plugin estimate"
BASIS_USER = "Your input"
BASIS_ASSUME = "Plugin assumption"
BASIS_BLANK = "Blank: you enter it"
BASIS_NONE = "Not given by this method"
BASIS_REF = "Reference only"


class HecRasError(ValueError):
    pass


# ============================================================================
# Export: hydrograph
# ============================================================================
def ras_interval_label(dt_min):
    """HEC-RAS data interval text ('5MIN', '1HOUR', ...) or None when the step is not one of
    the intervals the boundary-condition editor offers."""
    m = float(dt_min)
    if m not in RAS_INTERVALS_MIN:
        return None
    m = int(m)
    if m < 60:
        return "{}MIN".format(m)
    if m < 1440:
        return "{}HOUR".format(m // 60)
    return "1DAY"


def resample_hydrograph(hy, dt_min=DEFAULT_STEP_MIN):
    """The hydrograph on a regular step from time zero of the record: (t_hr, q_cfs, info).

    Values are linear interpolations of the record. The regular grid may miss a sharp peak, so
    the sample nearest the peak time is set to the exact peak discharge (the peak time moves by
    at most half a step; the volume grows a little, both reported in `info`)."""
    dt_min = float(dt_min)
    if not dt_min > 0:
        raise HecRasError("the time step must be greater than 0")
    dt = dt_min * 60.0
    t_src = np.asarray(hy.t_s, dtype=float)
    q_src = np.asarray(hy.q_cms, dtype=float)
    if len(t_src) < 2:
        raise HecRasError("the hydrograph needs at least two points")
    t_end = float(t_src[-1])
    n = int(math.ceil(t_end / dt - 1e-9)) + 1
    n = max(n, 2)
    t = np.arange(n) * dt
    q = np.clip(np.interp(t, t_src, q_src, right=0.0), 0.0, None)
    ip = int(np.argmax(q_src))
    peak = float(q_src[ip])
    k = min(int(round(float(t_src[ip]) / dt)), n - 1)
    if q.max() < peak:
        q[k] = peak
    q_cfs = q / CFS

    def vol(tt, qq):
        return float(np.trapezoid(qq, tt)) if hasattr(np, "trapezoid") else float(np.trapz(qq, tt))
    v0, v1 = vol(t_src, q_src), vol(t, q)
    info = {"n": n, "dt_min": dt_min, "interval": ras_interval_label(dt_min),
            "peak_cfs": peak / CFS, "peak_time_hr": float(t_src[ip]) / 3600.0,
            "peak_time_exported_hr": float(t[int(np.argmax(q))]) / 3600.0,
            "volume_acft": v0 / ACFT, "volume_exported_acft": v1 / ACFT,
            "volume_change_pct": 100.0 * (v1 - v0) / v0 if v0 > 0 else 0.0,
            "notes": []}
    if info["interval"] is None:
        info["notes"].append("{:g} min is not one of the data intervals HEC-RAS offers "
                             "(1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30 min, 1, 2, 3, 4, 6, 8, 12 "
                             "h, 1 day); pick one of those.".format(dt_min))
    if getattr(hy, "t_ref_s", 0.0):
        info["notes"].append(
            "Breach initiation is {:.3f} h into this record (a flood-scenario hydrograph starts "
            "with the inflow). Time zero of the block is the start of the record.".format(
                hy.t_ref_s / 3600.0))
    return t / 3600.0, q_cfs, info


def _field8(v, width=8):
    """A number in a `width`-character field: as many decimals (up to 2) as fit."""
    v = float(v)
    for d in (2, 1, 0):
        s = "{:.{}f}".format(v, d)
        if len(s) <= width:
            return s.rjust(width)
    return "{:.2e}".format(v).rjust(width)


def flow_hydrograph_block(q_cfs, per_line=10, width=8, interval=None):
    """The `.u##` text: 'Flow Hydrograph= N' and the values, `per_line` to a line in
    `width`-character fields. With `interval` ('5MIN') an 'Interval=' line goes first."""
    q = [float(x) for x in q_cfs]
    lines = []
    if interval:
        lines.append("Interval={}".format(interval))
    lines.append("Flow Hydrograph= {}".format(len(q)))
    for i in range(0, len(q), per_line):
        lines.append("".join(_field8(x, width) for x in q[i:i + per_line]))
    return "\n".join(lines) + "\n"


def write_hydrograph_files(hy, folder, dt_min=DEFAULT_STEP_MIN, stem="HECRAS Breach Hydrograph",
                           include_interval=False):
    """Writes '<stem>.txt' (the .u## block) and '<stem>.csv' (time_hr, discharge_cfs).
    Returns {'txt', 'csv', 'info'}."""
    t_hr, q_cfs, info = resample_hydrograph(hy, dt_min)
    os.makedirs(folder, exist_ok=True)
    txt = os.path.join(folder, stem + ".txt")
    with open(txt, "w") as f:
        f.write(flow_hydrograph_block(q_cfs, interval=info["interval"] if include_interval
                                      else None))
    csv_path = os.path.join(folder, stem + ".csv")
    with open(csv_path, "w") as f:
        f.write("time_hr,discharge_cfs\n")
        for t, q in zip(t_hr, q_cfs):
            f.write("{:.5f},{:.2f}\n".format(t, q))
    return {"txt": txt, "csv": csv_path, "info": info}


# ============================================================================
# Export: breach parameter table
# ============================================================================
BREACH_WEIR_COEF = 3.1        # the breach module's broad-crested weir, US units (Fread)


def _num(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return ("%.3f" % v).rstrip("0").rstrip(".")
    return str(v)


def breach_parameter_rows(dam, params, peak_cfs=None):
    """Rows (dicts item, value, units, basis, note) of the HEC-RAS breach editor.

    `dam` is a breach.DamData, `params` the parameters dict of the breach module
    (Hydrograph.params or the 'hydrograph' block of run_report.json). The centre station is
    always blank. Rows whose basis is 'Plugin estimate' are computed by regression equations
    and are not design values."""
    p = dict(params or {})
    src = str(p.get("breach_parameters") or "")
    rows = []

    def add(item, value, units, basis, note=""):
        rows.append({"item": item, "value": value, "units": units, "basis": basis,
                     "note": note})

    bottom = p.get("breach_bottom_ft")
    bottom_basis = BASIS_EST
    if bottom is None:
        bottom = (dam.breach_bottom_ft if dam.breach_bottom_ft is not None
                  else dam.top_of_dam_ft - dam.dam_height_ft)
    if dam.breach_bottom_ft is not None:
        bottom_basis = BASIS_USER
    hb = dam.top_of_dam_ft - bottom
    z = p.get("side_slope_h")
    avg = p.get("avg_breach_width_ft")
    b_bottom = p.get("bottom_width_ft")
    if b_bottom is None and avg is not None and z is not None:
        b_bottom = max(0.0, float(avg) - float(z) * hb)
    tf = p.get("formation_time_hr")
    mode = str(p.get("failure_mode") or "").lower()

    add("Breach center station", None, "station", BASIS_BLANK,
        "Where the breach sits along the embankment; depends on your HEC-RAS geometry.")
    add("Final bottom elevation", bottom, "ft", bottom_basis,
        "Top of dam minus dam height unless a breach bottom was given; same datum as the DEM.")
    add("Final bottom width", b_bottom, "ft",
        BASIS_EST if b_bottom is not None else BASIS_NONE,
        "Average breach width {} ft minus side slope x breach height ({:.1f} ft){}.".format(
            _num(avg) if avg is not None else "?", hb,
            "; the average width is yours" if "width from user" in src else "")
        if b_bottom is not None else
        "This method gives no breach width; enter your own.")
    z_basis = BASIS_USER if "side slope from user" in src else (
        BASIS_EST if z is not None else BASIS_NONE)
    for side in ("Left", "Right"):
        add(side + " side slope", z, "H:1V", z_basis,
            "The plugin breaches a symmetric trapezoid, so left = right." if z is not None
            else "This method gives no side slope; enter your own.")
    add("Breach weir coefficient", BREACH_WEIR_COEF, "US units", BASIS_ASSUME,
        "The breach module's weir equation Q = 3.1 b h^1.5 + 2.45 z h^2.5 (Fread). The HEC-RAS "
        "default is 2.6; use 3.1 to match this plugin's hydrograph, or your reviewer's value.")
    if tf is not None:
        add("Full formation time", tf, "hr",
            BASIS_USER if "formation time from user" in src else BASIS_EST,
            "Time from breach initiation to the full final opening.")
    else:
        rise = p.get("rise_time_hr")
        add("Full formation time", None, "hr", BASIS_NONE,
            "TR-60 gives a peak discharge, not a formation time; the plugin's hydrograph rose "
            "over {} h. Enter your own.".format(_num(rise) if rise is not None else "?"))
    add("Breach progression", "Linear", "", BASIS_ASSUME,
        "The plugin lowers the crest and widens the bottom linearly over the formation time; "
        "HEC-RAS's sine-wave option gives a different hydrograph shape.")
    if mode:
        add("Breach mode", mode.capitalize(), "", BASIS_EST,
            "Froehlich parameters for a {} failure. The plugin routes the opening as a weir "
            "either way (except with the pipe phase).".format(mode))
    if mode == "piping":
        if p.get("pipe_initial_dia_ft") is not None:
            cd = p.get("pipe_cd")
            add("Piping coefficient", cd, "", BASIS_ASSUME,
                "Orifice coefficient used for the pipe phase.")
            add("Initial piping elevation", bottom + float(p["pipe_initial_dia_ft"]) / 2.0,
                "ft", BASIS_ASSUME,
                "Centre of the starting pipe ({} ft across) resting on the breach bottom."
                .format(_num(p["pipe_initial_dia_ft"])))
        else:
            add("Piping coefficient", None, "", BASIS_BLANK,
                "Only needed in HEC-RAS piping mode (its default is 0.5).")
            add("Initial piping elevation", None, "ft", BASIS_BLANK,
                "Only needed in HEC-RAS piping mode.")
    trig = p.get("breach_trigger")
    if trig == "time":
        add("Breach initiation trigger", "Set time", "", BASIS_USER,
            "The dam fails at a time you chose in the flood scenario.")
        add("Trigger value", p.get("breach_initiation_hr"), "hr after the start of the inflow",
            BASIS_USER, "")
    elif trig in ("peak", "wse"):
        add("Breach initiation trigger", "Water surface elevation", "", BASIS_EST
            if trig == "peak" else BASIS_USER,
            "Flood scenario: the dam fails at the highest pool level of the no-breach "
            "routing." if trig == "peak" else
            "Flood scenario: the dam fails when the pool reaches the elevation you chose.")
        add("Trigger value", p.get("pool_at_breach_ft"), "ft", BASIS_EST if trig == "peak"
            else BASIS_USER, "Pool elevation when the breach starts ({} h into the inflow)."
            .format(_num(p.get("breach_initiation_hr"))))
    else:
        add("Breach initiation trigger", "Water surface elevation", "", BASIS_ASSUME,
            "Sunny day (ND 5.1.1): the breach starts with the pool at the top of dam, or at "
            "the PMF maximum level when that is lower; start the HEC-RAS pool there.")
        add("Trigger value", p.get("starting_wse_ft"), "ft", BASIS_EST,
            "Starting water surface: {}.".format(p.get("starting_wse_rule") or "top of dam"))
    add("Top of dam", dam.top_of_dam_ft, "ft", BASIS_REF, "As given for the run.")
    if avg is not None:
        add("Average breach width", avg, "ft", BASIS_REF,
            "The width regression's result; HEC-RAS asks for the bottom width above.")
    peak = peak_cfs if peak_cfs is not None else p.get("peak_cfs")
    if peak is not None:
        add("Peak breach outflow (plugin)", peak, "cfs", BASIS_REF,
            "Compare this with the HEC-RAS result.")
    return rows


def write_breach_table(dam, params, folder, stem="HECRAS Breach Parameters", peak_cfs=None,
                       notes=None):
    """Writes '<stem>.csv' and '<stem>.md'. Returns {'csv', 'md', 'rows'}."""
    rows = breach_parameter_rows(dam, params, peak_cfs)
    os.makedirs(folder, exist_ok=True)
    csv_path = os.path.join(folder, stem + ".csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item", "value", "units", "basis", "note"])
        for r in rows:
            w.writerow([r["item"], _num(r["value"]), r["units"], r["basis"], r["note"]])
    src = (params or {}).get("breach_parameters") or "the run's breach method"

    def cell(s):
        return str(s).replace("|", "/")
    md = ["# HEC-RAS breach parameters: {}".format(dam.dam_id), "",
          "**These are the plugin's estimates**, not design values: they come from {} and from "
          "the dam data you gave, in US units. Rows marked *{}* are computed by regression "
          "equations; *{}* are choices this plugin made; *{}* is for you to fill in. Review "
          "them (geotechnical input, the breach method your reviewer expects) before using "
          "them in a submittal.".format(src, BASIS_EST, BASIS_ASSUME, BASIS_BLANK), "",
          "| Item | Value | Units | Basis | Note |", "|---|---|---|---|---|"]
    for r in rows:
        md.append("| {} | {} | {} | {} | {} |".format(
            cell(r["item"]), cell(_num(r["value"]) or "(blank)"), cell(r["units"]),
            cell(r["basis"]), cell(r["note"])))
    if notes:
        md += ["", "Notes from the run:", ""] + ["- " + cell(n) for n in notes]
    md.append("")
    md_path = os.path.join(folder, stem + ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    return {"csv": csv_path, "md": md_path, "rows": rows}


def load_run(dam_folder, model_folder=MODEL_FOLDER):
    """(Hydrograph, DamData, params) from a dam folder written by the workflow: the
    hydrograph CSV and run_report.json in `<dam folder>/<model folder>/`."""
    import json
    from . import breach
    base = os.path.join(dam_folder, model_folder)
    csv_path = os.path.join(base, HYDROGRAPH_CSV)
    rep_path = os.path.join(base, RUN_REPORT_JSON)
    missing = [p for p in (csv_path, rep_path) if not os.path.exists(p)]
    if missing:
        raise HecRasError("Not a finished breach run: missing {}".format(
            ", ".join(missing)))
    with open(rep_path) as f:
        report = json.load(f)
    if "dam" not in report or "hydrograph" not in report:
        raise HecRasError("{} has no 'dam' and 'hydrograph' entries".format(rep_path))
    try:
        hy = breach.read_user_hydrograph(csv_path)
    except breach.BreachInputError as e:
        raise HecRasError(str(e))
    params = dict(report["hydrograph"])
    hy.method = params.get("method", hy.method)
    hy.params = params
    hy.t_ref_s = float(params.get("breach_initiation_hr") or 0.0) * 3600.0
    dam = breach.dam_from_dict(report["dam"])
    return hy, dam, params


def export_run(dam_folder, out_folder=None, dt_min=DEFAULT_STEP_MIN,
               model_folder=MODEL_FOLDER):
    """Everything the export algorithm writes for a finished run."""
    hy, dam, params = load_run(dam_folder, model_folder)
    out = out_folder or os.path.join(dam_folder, "_hecras")
    h = write_hydrograph_files(hy, out, dt_min)
    t = write_breach_table(dam, params, out, peak_cfs=params.get("peak_cfs"),
                           notes=h["info"]["notes"])
    return {"hydrograph_txt": h["txt"], "hydrograph_csv": h["csv"], "table_csv": t["csv"],
            "table_md": t["md"], "info": h["info"], "rows": t["rows"]}


# ============================================================================
# Import: HDF discovery
# ============================================================================
def _norm(s):
    return re.sub(r"\s+", " ", str(s).lower()).strip()


def _text(v):
    if isinstance(v, (bytes, np.bytes_)):
        return v.decode("utf-8", "replace")
    if isinstance(v, np.ndarray):
        return _text(v.reshape(-1)[0]) if v.size else ""
    return str(v)


def _attrs(obj):
    try:
        return {str(k): v for k, v in dict(obj.attrs).items()}
    except Exception:
        return {}


def _is_dataset(obj):
    return hasattr(obj, "shape") and hasattr(obj, "dtype")


def _open_hdf(hdf):
    """(file object, close function). `hdf` is a path, or an already open h5py-like file."""
    if hasattr(hdf, "visititems"):
        return hdf, (lambda: None)
    try:
        import h5py
    except ImportError:
        raise HecRasError(
            "Reading HEC-RAS results needs the Python package h5py, which QGIS does not "
            "include. Install it into QGIS's own Python (for example from the OSGeo4W Shell "
            "on Windows: python -m pip install h5py; on macOS: the python3 inside "
            "QGIS.app/Contents/MacOS/bin, with -m pip install h5py), then restart QGIS.")
    if not os.path.exists(str(hdf)):
        raise HecRasError("No such file: {}".format(hdf))
    try:
        f = h5py.File(str(hdf), "r")
    except OSError as e:
        raise HecRasError("{} is not a readable HDF5 file ({})".format(hdf, e))
    return f, f.close


class Catalog:
    """Every dataset of the file by path, with the attributes of the groups above it."""

    def __init__(self, f):
        self.datasets = []
        self.group_attrs = {"": _attrs(f)}

        def visit(name, obj):
            if _is_dataset(obj):
                self.datasets.append((name, obj))
            else:
                self.group_attrs[name] = _attrs(obj)
        f.visititems(visit)

    def find(self, keys, area=None, prefer=(), forbid=(), exact=False, under=None):
        """Datasets whose base name contains all `keys` (or equals the single key when
        `exact`), inside the area group when `area` is given and under a path containing
        `under`; best first: preferred path words, then the shortest base name."""
        keys = [_norm(k) for k in ([keys] if isinstance(keys, str) else keys)]
        out = []
        for path, obj in self.datasets:
            parts = [_norm(p) for p in path.split("/")]
            base = parts[-1]
            if exact:
                if base != keys[0]:
                    continue
            elif not all(k in base for k in keys):
                continue
            if any(fb in base for fb in forbid):
                continue
            if area is not None and _norm(area) not in parts[:-1]:
                continue
            npath = "/".join(parts)
            if under is not None and _norm(under) not in npath:
                continue
            rank = (0 if any(_norm(w) in npath for w in prefer) else 1, len(base), len(path))
            out.append((rank, path, obj))
        out.sort(key=lambda r: r[0])
        return [(p, o) for _, p, o in out]

    def units_of(self, path):
        """'us', 'si' or None from the 'Units' attribute of the dataset, its groups, the root."""
        obj = dict(self.datasets).get(path)
        chain = [_attrs(obj)] if obj is not None else []
        parts = path.split("/")
        for i in range(len(parts) - 1, -1, -1):
            chain.append(self.group_attrs.get("/".join(parts[:i]), {}))
        for at in chain:
            for k, v in at.items():
                if _norm(k) in ("units", "units system", "unit", "unit system"):
                    u = parse_units(_text(v))
                    if u:
                        return u
        return None

    def listing(self, limit=40):
        words = ("cell", "face", "water surface", "velocity", "elevation", "depth")
        rel = [p for p, _ in self.datasets if any(w in p.lower() for w in words)]
        shown = rel[:limit]
        s = "; ".join(shown) or "(nothing that looks like 2D results)"
        if len(rel) > limit:
            s += "; ... and {} more".format(len(rel) - limit)
        return "{} datasets in the file, relevant ones: {}".format(len(self.datasets), s)


def parse_units(text):
    """'us' for ft / US Customary, 'si' for m / SI Metric, else None."""
    toks = set(re.findall(r"[a-z]+", str(text).lower()))
    if toks & {"si", "metric", "m", "meter", "meters", "metre", "metres", "ms"}:
        return "si"
    if toks & {"us", "ft", "feet", "foot", "customary", "english", "imperial", "fps", "usft"}:
        return "us"
    return None


def _values(a):
    """Split a summary dataset into (values, time or None): (2, N) rows, (N, 2) columns or
    (N,) values."""
    a = np.asarray(a, dtype=np.float64)
    a = np.squeeze(a) if a.ndim > 2 else a
    if a.ndim == 1:
        return a, None
    if a.ndim != 2:
        raise HecRasError("unexpected shape {} for a summary dataset".format(a.shape))
    if a.shape[0] <= 2 and a.shape[1] > 2:
        return a[0], (a[1] if a.shape[0] == 2 else None)
    if a.shape[1] <= 2 and a.shape[0] > 2:
        return a[:, 0], (a[:, 1] if a.shape[1] == 2 else None)
    return a[0], (a[1] if a.shape[0] > 1 else None)


def _clean(a):
    a = np.array(a, dtype=np.float64)
    a[~np.isfinite(a) | (a <= -9998.0) | (a > 1e12)] = np.nan
    return a


def _read(ds):
    return np.asarray(ds[...])


def _time_factor_hr(catalog, path, obj):
    """Hours per unit of a RAS time dataset (days unless an attribute says otherwise)."""
    at = _attrs(obj)
    at.update({k: v for k, v in catalog.group_attrs.get(path.rsplit("/", 1)[0], {}).items()
               if k not in at})
    for k, v in at.items():
        if _norm(k) in ("time", "units", "unit", "time units"):
            s = _text(v).lower()
            if "sec" in s:
                return 1.0 / 3600.0
            if "min" in s:
                return 1.0 / 60.0
            if "hour" in s or s.strip() in ("hr", "hrs", "h"):
                return 1.0
            if "day" in s:
                return 24.0
    return 24.0


def discover(catalog, area=None):
    """The datasets to use for one 2D flow area. Raises HecRasError listing what was found
    when a required one is missing. Returns a dict of paths and objects."""
    centers = catalog.find("cells center coordinate")
    if not centers:
        raise HecRasError("No 'Cells Center Coordinate' dataset: is this a HEC-RAS 2D plan "
                          "HDF (.p##.hdf)? " + catalog.listing())
    areas = {}
    for path, obj in centers:
        parts = path.split("/")
        name = parts[-2] if len(parts) >= 2 else ""
        areas.setdefault(name, (path, obj))
    notes = []
    if area:
        hit = [n for n in areas if _norm(n) == _norm(area)]
        if not hit:
            raise HecRasError("2D flow area '{}' not found; the file has: {}".format(
                area, ", ".join(areas)))
        name = hit[0]
    else:
        name = max(areas, key=lambda n: areas[n][1].shape[0])
        if len(areas) > 1:
            notes.append("The file has {} 2D flow areas ({}); using '{}', the largest. Name "
                         "one to choose another.".format(len(areas), ", ".join(areas), name))
    cpath, cobj = areas[name]
    geo = cpath.rsplit("/", 1)[0]
    res = {"area": name, "areas": list(areas), "notes": notes, "centers": (cpath, cobj)}

    def pick(keys, **kw):
        hits = catalog.find(keys, area=name, **kw)
        same = [h for h in hits if h[0].rsplit("/", 1)[0] == geo]
        return (same or hits or [None])[0]

    res["min_elev"] = pick("cells minimum elevation")
    res["face_cells"] = pick("faces cell indexes")
    summ = ("summary output",)
    res["max_wse"] = pick("maximum water surface", prefer=summ, forbid=("error",))
    res["cell_vel"] = pick(("maximum", "velocity"), prefer=summ, forbid=("face", "error"))
    res["face_vel"] = pick(("maximum", "face", "velocity"), prefer=summ, forbid=("error",))
    res["ts_wse"] = pick("water surface", prefer=("unsteady time series",), exact=True,
                         under="time series")
    times = catalog.find("time", exact=True, under="time series")
    res["ts_time"] = times[0] if times else None
    missing = [label for key, label in (("min_elev", "Cells Minimum Elevation"),
                                        ("max_wse", "Maximum Water Surface"))
               if res[key] is None]
    if missing:
        raise HecRasError("Area '{}': cannot find {}. {}".format(
            name, " and ".join(missing), catalog.listing()))
    return res


# ============================================================================
# Import: mesh results
# ============================================================================
class Mesh:
    """Per-cell results of one 2D flow area in US units, cells as points."""

    def __init__(self, x, y, fields, info, crs_wkt=None):
        self.x, self.y = x, y
        self.fields = fields          # name -> (N,) array; NaN where dry or unknown
        self.info = info
        self.crs_wkt = crs_wkt
        self._nn = None

    @property
    def n(self):
        return len(self.x)

    def nn_distance(self):
        """Distance from each cell centre to the nearest other one, and the median."""
        if self._nn is None:
            self._nn = cell_spacing(self.x, self.y)
        return self._nn


def _unit_factor(u):
    return 1.0 / FT if u == "si" else 1.0


def read_mesh(hdf, area=None, units=None, arrival_depth_ft=1.0, t0_hr=0.0, wet_ft=0.01,
              want_arrival=True):
    """The maximum results of a 2D flow area as a Mesh (fields depth, wse, velocity,
    arrival in ft, ft, ft/s, hours; time_of_max in hours when the file has it).

    units: None to read them from the file (default US when absent), 'us' or 'si'.
    Cells drier than `wet_ft` are NaN in every field. Arrival is the first time the depth
    reaches `arrival_depth_ft` (interpolated between output steps) minus `t0_hr`, from the
    Water Surface time series; without it there is no arrival."""
    f, close = _open_hdf(hdf)
    try:
        return _read_mesh(f, area, units, arrival_depth_ft, t0_hr, wet_ft, want_arrival)
    finally:
        close()


def _read_mesh(f, area, units, arrival_depth_ft, t0_hr, wet_ft, want_arrival):
    cat = Catalog(f)
    lay = discover(cat, area)
    notes = list(lay["notes"])
    used = {}

    def unit_for(item, what):
        if units in ("us", "si"):
            return units
        u = cat.units_of(item[0])
        if u is None:
            notes.append("No units attribute found for {}; taken as US customary. Pass "
                         "units='si' if the model was in metric.".format(what))
            return "us"
        return u

    wse_raw, t_max_raw = _values(_read(lay["max_wse"][1]))
    used["max_wse"] = lay["max_wse"][0]
    n = len(wse_raw)
    xy = np.asarray(_read(lay["centers"][1]), dtype=np.float64)
    if xy.ndim != 2 or xy.shape[1] < 2 or xy.shape[0] < n:
        raise HecRasError("Cells Center Coordinate has shape {} but Maximum Water Surface has "
                          "{} cells".format(xy.shape, n))
    x, y = xy[:n, 0], xy[:n, 1]
    used["centers"] = lay["centers"][0]
    elev_raw = _clean(_read(lay["min_elev"][1]).reshape(-1))
    used["min_elev"] = lay["min_elev"][0]
    if len(elev_raw) < n:
        raise HecRasError("Cells Minimum Elevation has {} values, fewer than the {} cells"
                          .format(len(elev_raw), n))
    elev_raw = elev_raw[:n]
    u_wse = unit_for(lay["max_wse"], "Maximum Water Surface")
    u_elev = unit_for(lay["min_elev"], "Cells Minimum Elevation")
    wse = _clean(wse_raw) * _unit_factor(u_wse)
    elev = elev_raw * _unit_factor(u_elev)
    with np.errstate(invalid="ignore"):
        depth = np.clip(wse - elev, 0.0, None)
        wet = depth >= wet_ft
    fields = {}
    dry = np.nan

    def masked(a):
        return np.where(wet, a, dry)
    fields["depth"] = masked(depth)
    fields["wse"] = masked(wse)

    # velocity: per cell if the file has it, else the face values averaged onto the cells
    vel = None
    if lay["cell_vel"] is not None:
        v, _ = _values(_read(lay["cell_vel"][1]))
        if len(v) >= n:
            vel = np.abs(_clean(v[:n])) * _unit_factor(unit_for(lay["cell_vel"], "cell velocity"))
            used["velocity"] = lay["cell_vel"][0]
    if vel is None and lay["face_vel"] is not None and lay["face_cells"] is not None:
        fv, _ = _values(_read(lay["face_vel"][1]))
        fc = np.asarray(_read(lay["face_cells"][1]))
        if fc.ndim == 2 and fc.shape[1] >= 2:
            m = min(len(fv), len(fc))
            if len(fv) != len(fc):
                notes.append("Maximum Face Velocity has {} faces but Faces Cell Indexes {}; "
                             "the first {} were used.".format(len(fv), len(fc), m))
            fv = np.abs(_clean(fv[:m])) * _unit_factor(unit_for(lay["face_vel"],
                                                                "face velocity"))
            s = np.zeros(n)
            c = np.zeros(n)
            for col in (0, 1):
                idx = fc[:m, col].astype(np.int64)
                ok = (idx >= 0) & (idx < n) & np.isfinite(fv)
                s += np.bincount(idx[ok], weights=fv[ok], minlength=n)
                c += np.bincount(idx[ok], minlength=n)
            with np.errstate(invalid="ignore", divide="ignore"):
                vel = np.where(c > 0, s / np.maximum(c, 1), np.nan)
            used["velocity"] = lay["face_vel"][0] + " (faces averaged to cells)"
            notes.append("Velocity is the mean of the maximum face velocities around each "
                         "cell, an approximation of the cell's maximum velocity.")
    if vel is not None:
        fields["velocity"] = masked(vel)
    else:
        notes.append("No maximum velocity found (need 'Maximum Face Velocity' with 'Faces "
                     "Cell Indexes', or a per-cell maximum velocity); no velocity raster.")

    # time of the maximum, and the arrival time from the time series
    if t_max_raw is not None:
        fields["time_of_max"] = masked(_clean(t_max_raw[:n]) * _time_factor_hr(
            cat, lay["max_wse"][0], lay["max_wse"][1]))
    if want_arrival and lay["ts_wse"] is not None and lay["ts_time"] is not None:
        tobj = lay["ts_time"]
        t_hr = np.asarray(_read(tobj[1]), dtype=np.float64).reshape(-1) * _time_factor_hr(
            cat, tobj[0], tobj[1])
        sh = lay["ts_wse"][1].shape
        if len(sh) == 2 and sh[0] == len(t_hr) and sh[1] >= n:
            thr = float(arrival_depth_ft) / _unit_factor(u_wse)
            arr = _first_reach(lay["ts_wse"][1], t_hr, elev_raw * _unit_factor(u_elev)
                               / _unit_factor(u_wse), thr, n) - float(t0_hr)
            fields["arrival"] = masked(np.clip(arr, 0.0, None))
            used["arrival"] = lay["ts_wse"][0]
        else:
            notes.append("The Water Surface time series has shape {} for {} times and {} "
                         "cells; no arrival time.".format(sh, len(t_hr), n))
    elif want_arrival:
        notes.append("No Water Surface time series in the file: no arrival time (the time "
                     "of the maximum is written instead when it exists).")
    info = {"area": lay["area"], "areas": lay["areas"], "n_cells": n, "units": u_wse,
            "used": used, "notes": notes, "arrival_depth_ft": float(arrival_depth_ft),
            "t0_hr": float(t0_hr)}
    crs = None
    for k, v in cat.group_attrs.get("", {}).items():
        if _norm(k) in ("projection", "projection wkt", "crs"):
            crs = _text(v) or None
    return Mesh(x, y, fields, info, crs)


def _first_reach(ts, t_hr, elev, thr, n):
    """Per cell the first time (hours) the depth (water surface minus `elev`) reaches `thr`,
    interpolated between output steps; NaN when it never does. Reads a few rows at a time."""
    nt = ts.shape[0]
    out = np.full(n, np.nan)
    rows = max(2, int(4_000_000 // max(n, 1)))
    prev_d, prev_t = None, None
    for i in range(0, nt, rows):
        blk = _clean(np.asarray(ts[i:i + rows, :n], dtype=np.float64)) - elev[None, :]
        tt = t_hr[i:i + rows]
        if prev_d is not None:
            blk = np.vstack([prev_d[None, :], blk])
            tt = np.concatenate([[prev_t], tt])
        un = np.flatnonzero(np.isnan(out))
        if not len(un):
            break
        d = blk[:, un]
        with np.errstate(invalid="ignore"):
            hit = d >= thr
        any_hit = hit.any(axis=0)
        if any_hit.any():
            cols = np.flatnonzero(any_hit)
            j = hit[:, cols].argmax(axis=0)
            d1 = d[j, cols]
            t1 = tt[j]
            jm = np.maximum(j - 1, 0)
            d0 = d[jm, cols]
            t0 = tt[jm]
            with np.errstate(invalid="ignore", divide="ignore"):
                frac = np.where(np.isfinite(d0) & (d1 > d0) & (j > 0), (thr - d0) / (d1 - d0),
                                1.0)
            out[un[cols]] = np.where(j > 0, t0 + np.clip(frac, 0.0, 1.0) * (t1 - t0), t1)
        prev_d, prev_t = blk[-1].copy(), tt[-1]
    return out


# ============================================================================
# Import: unstructured cells -> regular raster (pure-numpy nearest cell)
# ============================================================================
class SpatialHash:
    """Uniform-grid spatial hash for nearest-point queries (a KD-tree substitute)."""

    def __init__(self, x, y, h):
        self.x = np.asarray(x, dtype=np.float64)
        self.y = np.asarray(y, dtype=np.float64)
        n = len(self.x)
        self.x0, self.y0 = float(self.x.min()), float(self.y.min())
        w = float(self.x.max()) - self.x0
        ht = float(self.y.max()) - self.y0
        h = float(h) if h > 0 else 1.0
        while True:
            nx, ny = int(w // h) + 1, int(ht // h) + 1
            if nx * ny <= 16 * n + 4096:
                break
            h *= 1.5
        self.h, self.nx, self.ny = h, nx, ny
        ix = ((self.x - self.x0) // h).astype(np.int64)
        iy = ((self.y - self.y0) // h).astype(np.int64)
        key = iy * nx + ix
        self.order = np.argsort(key, kind="stable")
        self.count = np.bincount(key, minlength=nx * ny)
        self.start = np.concatenate(([0], np.cumsum(self.count)[:-1]))

    def nearest(self, qx, qy, max_dist=None, exclude=None):
        """(index of the nearest point, distance) for each query; index -1 and distance inf
        when none lies within `max_dist`. `exclude`: per query, a point index to ignore (the
        query's own, for neighbour distances)."""
        qx = np.asarray(qx, dtype=np.float64)
        qy = np.asarray(qy, dtype=np.float64)
        m = len(qx)
        h = self.h
        best_d2 = np.full(m, np.inf)
        best_i = np.full(m, -1, dtype=np.int64)
        qix = np.floor((qx - self.x0) / h).astype(np.int64)
        qiy = np.floor((qy - self.y0) / h).astype(np.int64)
        active = np.ones(m, dtype=bool)
        if max_dist is None:
            rmax = min(max(self.nx, self.ny) + 1, 64)
        else:
            rmax = int(math.ceil(max_dist / h))
            far = ((qx < self.x0 - max_dist) | (qx > self.x0 + self.nx * h + max_dist)
                   | (qy < self.y0 - max_dist) | (qy > self.y0 + self.ny * h + max_dist))
            active[far] = False
        for r in range(rmax + 1):
            idx = np.flatnonzero(active)
            if not len(idx):
                break
            for ox in range(-r, r + 1):
                for oy in range(-r, r + 1):
                    if max(abs(ox), abs(oy)) != r:
                        continue
                    cx, cy = qix[idx] + ox, qiy[idx] + oy
                    ok = (cx >= 0) & (cx < self.nx) & (cy >= 0) & (cy < self.ny)
                    if not ok.any():
                        continue
                    sel = idx[ok]
                    k = cy[ok] * self.nx + cx[ok]
                    cnt, st = self.count[k], self.start[k]
                    for j in range(int(cnt.max())):
                        s2 = cnt > j
                        qi = sel[s2]
                        cand = self.order[st[s2] + j]
                        d2 = (self.x[cand] - qx[qi]) ** 2 + (self.y[cand] - qy[qi]) ** 2
                        if exclude is not None:
                            d2 = np.where(cand == exclude[qi], np.inf, d2)
                        better = d2 < best_d2[qi]
                        best_d2[qi[better]] = d2[better]
                        best_i[qi[better]] = cand[better]
            active &= ~(best_d2 <= (r * h) ** 2)
        d = np.sqrt(best_d2)
        if max_dist is not None:
            best_i[d > max_dist] = -1
            d[d > max_dist] = np.inf
        best_i[~np.isfinite(d)] = -1
        return best_i, d


def cell_spacing(x, y, chunk=250000):
    """(nn, median): distance from each point to its nearest neighbour, and the median."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    n = len(x)
    if n < 2:
        raise HecRasError("the mesh has fewer than two cells")
    w, ht = float(x.max() - x.min()), float(y.max() - y.min())
    if w == 0 and ht == 0:
        raise HecRasError("all cell centres are at the same place")
    area = w * ht
    h0 = math.sqrt(area / n) if area > 0 else max(w, ht) / n
    samp = np.linspace(0, n - 1, min(n, 2000)).astype(np.int64)
    _, d = SpatialHash(x, y, h0).nearest(x[samp], y[samp], exclude=samp)
    ok = np.isfinite(d) & (d > 0)
    med0 = float(np.median(d[ok])) if ok.any() else h0
    hs = SpatialHash(x, y, 2.0 * med0)
    nn = np.full(n, np.inf)
    for i in range(0, n, chunk):
        idx = np.arange(i, min(n, i + chunk))
        nn[idx] = hs.nearest(x[idx], y[idx], exclude=idx)[1]
    good = np.isfinite(nn) & (nn > 0)
    med = float(np.median(nn[good])) if good.any() else med0
    nn = np.where(good, nn, med)
    return nn, med


def _round_cell(s):
    """Median spacing rounded: whole units from 1 up, two significant figures below."""
    return float(max(1, round(s))) if s >= 1 else float("{:.2g}".format(s))


def grid_spec(mesh, cell_size=None, extent=None):
    """(x0, y0_top, dx, nx, ny) for the raster. Default: the cells' extent plus half a
    spacing, snapped to multiples of the cell size, cell size = median spacing rounded."""
    nn, med = mesh.nn_distance()
    dx = float(cell_size) if cell_size else _round_cell(med)
    if not dx > 0:
        raise HecRasError("the cell size must be greater than 0")
    if extent:
        xmin, ymin, xmax, ymax = (float(v) for v in extent)
        x0, ytop = xmin, ymax
    else:
        xmin, xmax = float(mesh.x.min()) - 0.5 * med, float(mesh.x.max()) + 0.5 * med
        ymin, ymax = float(mesh.y.min()) - 0.5 * med, float(mesh.y.max()) + 0.5 * med
        x0, ytop = math.floor(xmin / dx) * dx, math.ceil(ymax / dx) * dx
        ymin = math.floor(ymin / dx) * dx
    if not (xmax > x0 and ytop > ymin):
        raise HecRasError("the extent is empty")
    nx, ny = int(math.ceil((xmax - x0) / dx - 1e-9)), int(math.ceil((ytop - ymin) / dx - 1e-9))
    if nx * ny > 60_000_000:
        raise HecRasError("a {} x {} grid is too large; use a larger cell size or a smaller "
                          "extent".format(nx, ny))
    return x0, ytop, dx, nx, ny


def rasterise(mesh, x0, ytop, dx, nx, ny, names=None, reach=0.75, block=400000):
    """Fields of the mesh on a regular grid by nearest cell centre. A grid cell farther than
    `reach` x the spacing of the nearest cell (the cell's distance to its own nearest
    neighbour) from every centre is outside the mesh: NaN. Returns {name: (ny, nx)}.

    Two passes keep empty parts of the grid cheap: first every cell within 1.5 x the typical
    reach, then, for what is left, only the cells whose own reach is larger (coarse cells)."""
    nn, med = mesh.nn_distance()
    radius = np.maximum(reach * nn, 0.25 * med)
    cap = 20.0 * med
    r1 = min(1.5 * float(np.median(radius)), cap)
    tiers = [(np.arange(mesh.n), r1)]
    big = np.flatnonzero(radius > r1)
    if len(big):
        tiers.append((big, min(float(radius[big].max()), cap)))
    hashes = [SpatialHash(mesh.x[ids], mesh.y[ids], 2.0 * float(np.median(nn[ids])))
              for ids, _ in tiers]
    names = [k for k in (names or list(mesh.fields)) if k in mesh.fields]
    out = {k: np.full((ny, nx), np.nan) for k in names}
    xs = x0 + (np.arange(nx) + 0.5) * dx
    rows = max(1, block // max(nx, 1))
    covered = 0
    for r0 in range(0, ny, rows):
        r1_ = min(ny, r0 + rows)
        ys = ytop - (np.arange(r0, r1_) + 0.5) * dx
        qx = np.tile(xs, r1_ - r0)
        qy = np.repeat(ys, nx)
        cell = np.full(len(qx), -1, dtype=np.int64)
        todo = np.arange(len(qx))
        for (ids, md), hs in zip(tiers, hashes):
            idx, dist = hs.nearest(qx[todo], qy[todo], max_dist=md)
            gi = np.where(idx >= 0, ids[np.maximum(idx, 0)], -1)
            ok = (gi >= 0) & (dist <= radius[np.maximum(gi, 0)])
            cell[todo[ok]] = gi[ok]
            todo = todo[~ok]
            if not len(todo):
                break
        ok = cell >= 0
        covered += int(ok.sum())
        for k in names:
            v = np.where(ok, mesh.fields[k][np.maximum(cell, 0)], np.nan)
            out[k][r0:r1_, :] = v.reshape(r1_ - r0, nx)
    if not covered:
        raise HecRasError("The mesh does not overlap the grid (mesh x {:.1f} to {:.1f}, y "
                          "{:.1f} to {:.1f}; grid x {:.1f} to {:.1f}, y {:.1f} to {:.1f}). "
                          "Check that both are in the same coordinate system.".format(
                              mesh.x.min(), mesh.x.max(), mesh.y.min(), mesh.y.max(), x0,
                              x0 + nx * dx, ytop - ny * dx, ytop))
    return out


FIELD_NAMES = ("depth", "wse", "velocity", "arrival", "time_of_max")


def to_grids(mesh, cell_size=None, extent=None, crs_wkt=None):
    """{name: Grid} on a regular north-up grid; info has the grid spec."""
    x0, ytop, dx, nx, ny = grid_spec(mesh, cell_size, extent)
    arrays = rasterise(mesh, x0, ytop, dx, nx, ny)
    crs = crs_wkt or mesh.crs_wkt
    grids = {k: Grid(a, x0, ytop, dx, crs_wkt=crs) for k, a in arrays.items()}
    info = dict(mesh.info)
    info.update({"cell_size": dx, "nx": nx, "ny": ny, "x0": x0, "y_top": ytop,
                 "median_spacing": mesh.nn_distance()[1]})
    return grids, info


def import_results(hdf, cell_size=None, extent=None, area=None, **kw):
    """read_mesh + to_grids: ({name: Grid}, info)."""
    mesh = read_mesh(hdf, area=area, **kw)
    return to_grids(mesh, cell_size, extent)


def import_as_reference(hdf_path, like_grid, area=None, **kw):
    """The HEC-RAS maximum results on the grid of `like_grid` (a core Grid; the same
    coordinate system as the HEC-RAS geometry is assumed), as arrays of the same shape for
    benchmark.compare_fields: {'depth', 'wse', 'velocity', 'arrival'} in ft, ft, ft/s and
    hours (NaN where dry or outside the mesh). Fields the file cannot supply are left out.
    Keyword arguments go to read_mesh (units, arrival_depth_ft, t0_hr, wet_ft)."""
    mesh = read_mesh(hdf_path, area=area, **kw)
    arrays = rasterise(mesh, like_grid.x0, like_grid.y0, like_grid.dx, like_grid.nx,
                       like_grid.ny, names=("depth", "wse", "velocity", "arrival"))
    return arrays
