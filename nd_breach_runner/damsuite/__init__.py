"""damsuite - the shared package of the ND dam suite (ND Dam Hazard Classification Helper,
ND Dam Breach Runner, ND Dam Breach Screening).

Pure Python 3.9+ standard library: no QGIS, no numpy, no Qt. Importable anywhere (gdalsafe
imports osgeo only when one of its helpers is called).

The master copy lives in the suite folder; each plugin carries a byte-identical copy as a
sub-package (`<plugin>/damsuite/`) written by `sync_damsuite.py`, which also writes
`damsuite/FILES.json` = {file name: sha256 of its bytes} for every .py file of the package.
Never edit a vendored copy: edit the master and run the sync.

Modules:
  dam_record  one record per dam (<dam folder>/dam.json) shared by the three plugins
  versions    version checks between the plugins at start-up
  chain       model -> classification fingerprint chain (raster digests vs the runner's
              run_manifest.json)
  tm11_curves the ND REG_05.2026 Fig. 5.1 / 5.2 depth-velocity flood danger curves (one
              table for all three plugins, digitized by the engineer)
  suite_home  one view of a Dams folder (next action per dam, which plugin) for the three
              plugins' "Dam suite home" windows
  gdalsafe    GDAL calls that behave the same with GDAL's exceptions on or off (1.3.0: the
              plugins no longer switch that process-wide setting)
  settings    the suite settings file (1.4.0): one table of every engineer-chosen value of the
              three plugins, read as the tools' defaults and edited in the popups' grids
  xlsx_lite   a dependency-free .xlsx writer / reader for that file (1.4.0)
  units       feet / acres / acre-feet <-> metres for the user-facing values (1.4.0)
  suitedata   the suite data folder and Dams folder set once per computer (1.5.0)
  services    the WMS / WMTS / WCS / ArcGIS REST servers, the user's own, and the data
              source picked for each role (1.5.0)
  terrain_cache  the shared terrain tile cache (1.5.0)
  damdata     "Get data for this dam": <dam>/data/ and dam.json's data section, and the
              autofill of tool inputs from it (1.5.0)
  offline_pack  the statewide offline data pack reader / writer (moved from ND Dam Hazard,
              1.5.0)
  suitegpkg   EPSG:5070 projection and a pure-Python GeoPackage writer / reader (moved from
              ND Dam Hazard, 1.5.0)
  keepawake   keeps the computer from sleeping while a flood model runs, beside the solver
              (1.8.0)

`selfcheck()` compares this package's files with FILES.json and returns a list of problems
("missing: x.py", "changed: x.py", "unexpected file: x.py"); [] means the copy is intact.
The comparison is on raw bytes (a CRLF conversion counts as a change).

Nothing here has been run inside a real QGIS; the QGIS-facing part (versions.installed) is
written defensively and returns None on any failure.
"""
import hashlib
import json
import os

from . import chain, dam_record, gdalsafe, settings, suite_home, tm11_curves, units, versions, xlsx_lite   # noqa: F401,E501  (sub-modules, relative imports only)
from . import offline_pack, suitegpkg, suitedata, services, terrain_cache, damdata   # noqa: F401,E501  (1.5.0)
from . import keepawake   # noqa: F401  (1.8.0)

SUITE_VERSION = "1.8.0"
FILES_NAME = "FILES.json"

__all__ = ["SUITE_VERSION", "FILES_NAME", "selfcheck", "package_files", "chain", "dam_record",
           "gdalsafe", "settings", "suite_home", "tm11_curves", "units", "versions", "xlsx_lite",
           "offline_pack", "suitegpkg", "suitedata", "services", "terrain_cache", "damdata",
           "keepawake"]


def package_files(folder=None):
    """{file name: sha256} of the .py files directly in the package folder (sorted)."""
    folder = folder or os.path.dirname(os.path.abspath(__file__))
    out = {}
    for name in sorted(os.listdir(folder)):
        p = os.path.join(folder, name)
        if name.endswith(".py") and os.path.isfile(p):
            with open(p, "rb") as f:
                out[name] = hashlib.sha256(f.read()).hexdigest()
    return out


def selfcheck(folder=None):
    """Problems of this package copy against its FILES.json; [] = OK.

    `folder` defaults to this package's own folder (the optional argument is for tests)."""
    folder = folder or os.path.dirname(os.path.abspath(__file__))
    fpath = os.path.join(folder, FILES_NAME)
    if not os.path.isfile(fpath):
        return ["FILES.json missing"]
    try:
        with open(fpath, encoding="utf-8") as f:
            expected = json.load(f)
        if not isinstance(expected, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        return ["FILES.json unreadable: {}".format(e)]
    actual = package_files(folder)
    problems = []
    for name in sorted(expected):
        if name not in actual:
            problems.append("missing: {}".format(name))
        elif actual[name] != expected[name]:
            problems.append("changed: {}".format(name))
    for name in sorted(set(actual) - set(expected)):
        problems.append("unexpected file: {}".format(name))
    return problems
