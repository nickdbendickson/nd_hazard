"""North Dakota data conventions: land-cover roughness, dam-inventory field
names, the per-dam input folder, and where the models are installed.

Nothing here needs a network; it works on the local files the user has.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys

# --------------------------------------------------------------------------------------
# NLCD land cover -> Manning's n (floodplain, shallow flow)
# --------------------------------------------------------------------------------------
# Typical values for overland flow drawn from the usual hydraulic references
# (Chow 1959; HEC-RAS 2D manual, Table 3-1 land-cover mapping; Kalyanapu et al.
# 2009). They are starting points: calibrate against a known flood where you
# can, and change any of them with the `overrides` argument or in the tool.
NLCD_N = {
    11: 0.035,   # open water
    12: 0.030,   # perennial ice / snow
    21: 0.035,   # developed, open space (lawns, parks)
    22: 0.080,   # developed, low intensity
    23: 0.100,   # developed, medium intensity
    24: 0.150,   # developed, high intensity
    31: 0.030,   # barren land
    41: 0.100,   # deciduous forest
    42: 0.110,   # evergreen forest
    43: 0.100,   # mixed forest
    51: 0.050,   # dwarf scrub
    52: 0.050,   # shrub / scrub
    71: 0.035,   # grassland / herbaceous
    72: 0.035,   # sedge / herbaceous
    73: 0.030,   # lichens
    74: 0.030,   # moss
    81: 0.035,   # pasture / hay
    82: 0.040,   # cultivated crops
    90: 0.100,   # woody wetlands
    95: 0.070,   # emergent herbaceous wetlands
}

NLCD_NAMES = {
    11: "Open water", 12: "Perennial ice/snow", 21: "Developed, open space",
    22: "Developed, low intensity", 23: "Developed, medium intensity",
    24: "Developed, high intensity", 31: "Barren land", 41: "Deciduous forest",
    42: "Evergreen forest", 43: "Mixed forest", 51: "Dwarf scrub", 52: "Shrub/scrub",
    71: "Grassland/herbaceous", 72: "Sedge/herbaceous", 73: "Lichens", 74: "Moss",
    81: "Pasture/hay", 82: "Cultivated crops", 90: "Woody wetlands",
    95: "Emergent herbaceous wetlands"}


def manning_lut(overrides=None, default=0.06):
    """{NLCD code: n}; `overrides` may be a dict or a JSON string of one."""
    lut = dict(NLCD_N)
    if isinstance(overrides, str) and overrides.strip():
        try:
            overrides = json.loads(overrides)
        except ValueError:
            raise ValueError("The n overrides must be JSON like {\"82\": 0.05, \"23\": 0.12}")
    for k, v in (overrides or {}).items():
        lut[int(k)] = float(v)
    lut["default"] = float(default)
    return lut


def nlcd_to_manning(codes, lut=None):
    """Array of NLCD class codes -> array of n (NaN codes and unknown classes
    get the default)."""
    import numpy as np
    lut = lut or manning_lut()
    a = np.asarray(codes, dtype=float)
    out = np.full(a.shape, float(lut.get("default", 0.06)))
    ok = np.isfinite(a)
    ai = np.where(ok, a, -1).astype(int)
    for code, n in lut.items():
        if code == "default":
            continue
        out[ok & (ai == int(code))] = float(n)
    return out


def lut_table(lut=None):
    lut = lut or manning_lut()
    return [(k, NLCD_NAMES.get(k, "class {}".format(k)), v) for k, v in sorted(
        (k, v) for k, v in lut.items() if k != "default")]


# --------------------------------------------------------------------------------------
# dam-inventory field names
# --------------------------------------------------------------------------------------
# Normalised (lower case, letters and digits only) names to look for, most likely first. The
# North Dakota State Water Commission inventory and the National Inventory of Dams
# both use variants of these; the match is by name, so check what it picked. 3.13.0: the ND DWR
# inventory's max_pool_vol / max_pool_area (at the top of dam). Its max_pool_elev is on a local
# datum, so it is not a top-of-dam elevation here.
FIELD_SYNONYMS = {
    "F_ID": ["nidid", "nid_id", "damid", "dam_id", "nationalid", "ndid", "id", "damnumber",
             "damno", "swcid", "structureid", "nidfedid"],
    "F_NAME": ["damname", "name", "dam_name", "structurename"],
    "F_HEIGHT": ["damheight", "nidheight", "height", "structuralheight", "hydraulicheight",
                 "damheightft", "heightft", "hgt"],
    "F_STORAGE": ["maxstorage", "nidstorage", "maxpoolvol", "storage", "maxstorageacft",
                  "storageacft",
                  "topofdamstorage", "maximumstorage", "storagetop", "normalstorage"],
    "F_TOP": ["topofdam", "topofdamelevation", "topofdamelev", "damcrest", "crestelevation",
              "crestelev", "topdam", "topelev"],
    "F_AREA": ["surfacearea", "surfaceareaac", "surfaceareaacres", "reservoirarea", "area",
               "maxsurfacearea", "surfarea", "maxpoolarea"],
    "F_PMF": ["pmfelevation", "pmfelev", "pmfwse", "pmfmaxelevation", "maxpmfelev",
              "pmflevel"],
    "F_BOTTOM": ["breachbottom", "breachbottomelevation", "breachbottomelev", "bottomelev",
                 "streambedelevation", "invertelevation"],
    "F_CREST": ["crestwidth", "topwidth", "damwidth", "crestwidthft"],
    "F_US": ["upstreamslope", "usslope", "upslope", "upstreamslopeh"],
    "F_DS": ["downstreamslope", "dsslope", "downslope", "downstreamslopeh"],
    "F_LENGTH": ["damlength", "crestlength", "length", "damlengthft", "embankmentlength"],
}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def suggest_fields(field_names):
    """Map F_* keys to the layer's field names by name. Returns
    ({key: field name}, [how each was found]); keys with no match are absent."""
    names = list(field_names)
    norm = {_norm(n): n for n in names}
    taken, out, how = set(), {}, []
    for key, syns in FIELD_SYNONYMS.items():
        hit = None
        for s in syns:
            n = norm.get(_norm(s))
            if n is not None and n not in taken:
                hit, why = n, "exact name"
                break
        if hit is None:
            # a field name that contains the synonym (e.g. 'DAM_HEIGHT_FT'), keys most specific first
            for s in syns:
                ns = _norm(s)
                if len(ns) < 5:
                    continue
                cand = [n for k, n in norm.items() if ns in k and n not in taken]
                if cand:
                    hit, why = sorted(cand, key=len)[0], "contains '{}'".format(s)
                    break
        if hit is not None:
            taken.add(hit)
            out[key] = hit
            how.append("{} -> {} ({})".format(key, hit, why))
    return out, how


# --------------------------------------------------------------------------------------
# per-dam input folder
# --------------------------------------------------------------------------------------
INPUT_FILES = {
    "pmf_inflow": ("pmf_inflow.csv", "PMF inflow hydrograph (time hr, discharge cfs)"),
    "spillway": ("spillway_rating.csv", "spillway rating (elevation ft, discharge cfs)"),
    "baseline": ("baseline_outflow.csv", "your no-breach outflow (own-hydrograph method)"),
    "breach": ("breach_hydrograph.csv", "your breach hydrograph (own-hydrograph method)"),
}
INPUT_SUBFOLDER = "inputs"


def dam_input_files(dam_folder):
    """{key: path} for the files present in <dam folder>/inputs/."""
    found = {}
    for key, (fn, _d) in INPUT_FILES.items():
        for base in (os.path.join(dam_folder, INPUT_SUBFOLDER), dam_folder):
            p = os.path.join(base, fn)
            if os.path.isfile(p):
                found[key] = p
                break
    return found


# --------------------------------------------------------------------------------------
# where the models are installed
# --------------------------------------------------------------------------------------
def _home_paths(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def lisflood_candidates():
    """Places a LISFLOOD-FP build usually ends up, existing files only."""
    c = []
    env = os.environ.get("NDBREACH_LISFLOOD")
    if env:
        c.append(env)
    w = shutil.which("lisflood")
    if w:
        c.append(w)
    pats = [
        _home_paths("lisflood-fp", "lisflood"),
        _home_paths("lisflood-fp*", "build", "lisflood"),
        _home_paths("lisflood*", "build", "lisflood"),
        _home_paths("src", "lisflood*", "build", "lisflood"),
        _home_paths("Documents", "lisflood*", "build", "lisflood"),
        _home_paths("Applications", "lisflood*", "lisflood"),
        _home_paths(".ndbreach", "bin", "lisflood"),
        _home_paths("Library", "Application Support", "ndbreach", "lisflood"),
        "/opt/lisflood*/build/lisflood", "/opt/lisflood*/lisflood",
        "/usr/local/bin/lisflood", "/opt/homebrew/bin/lisflood",
        # 3.11.0: Windows builds (lisflood.exe), where the install guide puts them
        _home_paths("lisflood-fp", "lisflood.exe"),
        _home_paths("lisflood-fp*", "build*", "Release", "lisflood.exe"),
        "C:\\lisflood-fp\\lisflood.exe", "C:\\lisflood*\\build*\\Release\\lisflood.exe",
    ]
    for p in pats:
        c += sorted(glob.glob(p))
    seen, out = set(), []
    for p in c:
        if p and p not in seen and os.path.isfile(p) and os.access(p, os.X_OK):
            seen.add(p)
            out.append(p)
    return out


def resolve_python(p):
    """Follow a 'python' symlink only when it points into the same folder (a conda env's
    bin/python -> python3.12). A venv's python points at the base interpreter outside the
    env; resolving that would lose the env's packages, so it is kept as given."""
    try:
        real = os.path.realpath(p)
    except (OSError, ValueError):
        return p
    if os.path.dirname(real) == os.path.dirname(os.path.abspath(p)):
        return real
    return p


def anuga_python_candidates():
    """Python interpreters of conda environments that look like ANUGA's."""
    c = []
    env = os.environ.get("NDBREACH_ANUGA_PYTHON")
    if env:
        c.append(env)
    try:                      # the private environment made by "Set up ANUGA" (core/anuga_setup.py)
        from . import anuga_setup
        c.append(anuga_setup.env_python())
    except Exception:
        pass
    roots = [_home_paths(d) for d in ("miniforge3", "mambaforge", "miniconda3", "anaconda3",
                                      "micromamba", ".conda")]
    roots += ["/opt/homebrew/Caskroom/miniforge/base", "/opt/homebrew/Caskroom/miniconda/base",
              "/opt/miniconda3", "/opt/anaconda3", "/usr/local/miniconda3", "/opt/conda"]
    roots += [os.environ.get("CONDA_PREFIX", "")]
    root_prefix = os.environ.get("MAMBA_ROOT_PREFIX")
    if root_prefix:
        roots.append(root_prefix)
    for r in roots:
        if not r:
            continue
        for p in sorted(glob.glob(os.path.join(r, "envs", "*anuga*", "bin", "python"))):
            c.append(p)
        if "anuga" in os.path.basename(r).lower():          # CONDA_PREFIX of an active env
            c.append(os.path.join(r, "bin", "python"))
    seen, out = set(), []
    for p in c:
        if not p or not os.path.isfile(p):
            continue
        real = resolve_python(p)
        if real not in seen:
            seen.add(real)
            out.append(real)
    return out


def conda_env_pythons(timeout=6.0):
    """Interpreters of every conda env, when `conda` is on the PATH (slow, so
    only used when the fixed places above found nothing)."""
    exe = shutil.which("conda") or shutil.which("mamba") or shutil.which("micromamba")
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "env", "list", "--json"], capture_output=True, text=True,
                           timeout=timeout)
        envs = json.loads(r.stdout).get("envs", [])
    except Exception:
        return []
    out = []
    for e in envs:
        p = os.path.join(e, "Scripts", "python.exe") if os.name == "nt" else os.path.join(
            e, "bin", "python")
        if os.path.isfile(p) and "anuga" in os.path.basename(e).lower():
            out.append(p)
    return out


def detect_lisflood(check=None, fallback=True):
    """First candidate that passes `check(path) -> bool` (all pass when None). 3.11.0: with
    `fallback`, when no LISFLOOD-FP is installed, the one bundled with the plugin for this
    platform, then the built-in engine (core/lisflood.py, resolve_exe)."""
    from . import lisflood as lfmod
    extra = [lfmod.bundled_exe(), lfmod.BUILTIN_EXE] if fallback else []
    for p in lisflood_candidates() + [e for e in extra if e]:
        try:
            if check is None or check(p):
                return p
        except Exception:
            continue
    return ""


def detect_anuga_python(check=None):
    for p in anuga_python_candidates() + conda_env_pythons():
        try:
            if check is None or check(p):
                return p
        except Exception:
            continue
    return ""
