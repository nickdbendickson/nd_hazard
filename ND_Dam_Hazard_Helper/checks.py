# -*- coding: utf-8 -*-
"""Input check before a run ("Check inputs" button; also run, without the
internet part, at the start of "Run everything").

Looks for the mistakes that otherwise surface late in a run or only in the
log: a water-surface-elevation raster given as depth, depths / velocities
that look like the wrong units, arrival times that look like minutes or
seconds instead of hours, velocity missing where there is depth, rasters in
a geographic or missing CRS, an empty flood, and (optionally) data services
that cannot be reached.

Each result is (level, text) with level ERROR (the run would fail or be
wrong), WARNING (check it) or OK.
"""

import math
import os

import numpy as np
from qgis.core import QgsRasterLayer

from .helpers import RasterSampler

ERROR, WARNING, OK = "ERROR", "WARNING", "OK"
SAMPLE_CELLS = 2000000


def _sample(sampler):
    """Values of every k-th row (about SAMPLE_CELLS cells): (values, xs, ys)
    of the valid cells."""
    if sampler is None or not sampler.ok:
        return np.empty(0), np.empty(0), np.empty(0)
    step = max(1, int(math.ceil(sampler.xsize * sampler.ysize / float(SAMPLE_CELLS))))
    vals, xs, ys = [], [], []
    for r in range(0, sampler.ysize, step):
        row = sampler.window_values(0, r, sampler.xsize, 1)
        if row is None:
            continue
        row = row.ravel()
        ok = np.isfinite(row)
        if not ok.any():
            continue
        cols = np.nonzero(ok)[0]
        x, y = sampler.cell_centres(cols, np.full(cols.size, r))
        vals.append(row[ok])
        xs.append(x)
        ys.append(y)
    if not vals:
        return np.empty(0), np.empty(0), np.empty(0)
    return np.concatenate(vals), np.concatenate(xs), np.concatenate(ys)


def _open(path, name):
    if not path:
        return None, None
    lyr = QgsRasterLayer(path, name)
    if not lyr.isValid():
        return lyr, None
    return lyr, RasterSampler(lyr)


def check_rasters(depth, velocity=None, arrival=None, dv=None, label=""):
    """Checks of one scenario's rasters (paths). [(level, text)]."""
    pre = "{}: ".format(label) if label else ""
    out = []
    opened = {}
    try:
        for key, path, name in (("depth", depth, "Depth"), ("vel", velocity, "Velocity"),
                                ("arr", arrival, "Arrival"), ("dv", dv, "D x V")):
            if not path:
                continue
            if not os.path.isfile(path):
                out.append((ERROR, "{}{} raster not found: {}".format(pre, name, path)))
                continue
            lyr, smp = _open(path, name)
            if smp is None or not smp.ok:
                out.append((ERROR, "{}{} raster cannot be read: {}".format(pre, name, path)))
                continue
            opened[key] = (lyr, smp, name)
            crs = lyr.crs()
            if not crs.isValid():
                out.append((WARNING, "{}{} raster has no coordinate system - it is assumed "
                                     "to match the other layers".format(pre, name)))
            elif crs.isGeographic():
                out.append((WARNING, "{}{} raster is in longitude / latitude ({}) - a "
                                     "projected CRS (UTM, ND State Plane) is recommended"
                            .format(pre, name, crs.authid())))
        if "depth" not in opened:
            if depth:
                return out
            return out + [(ERROR, pre + "no depth raster")]
        _l, dsmp, _n = opened["depth"]
        d, dx, dy = _sample(dsmp)
        wet = d > 0
        if not wet.any():
            out.append((ERROR, pre + "the depth raster has no cell with depth > 0"))
            return out
        dw = d[wet]
        med, mx = float(np.median(dw)), float(dw.max())
        if med > 300:
            out.append((ERROR, "{}depth values look like water-surface ELEVATIONS (median "
                               "{:,.0f}) - give the depth raster".format(pre, med)))
        elif mx > 150:
            out.append((WARNING, "{}maximum depth {:,.0f} ft - check the units (the plugin "
                                 "expects feet) and the raster".format(pre, mx)))
        else:
            out.append((OK, "{}depth: {:,} wet cells sampled, median {:.1f} ft, max {:.1f} "
                            "ft".format(pre, int(wet.sum()), med, mx)))
        if (d < 0).any() and float(d[d < 0].min()) > -9000:
            out.append((WARNING, "{}depth raster has negative values (min {:.1f}) - they are "
                                 "treated as dry".format(pre, float(d[d < 0].min()))))
        wx, wy = dx[wet], dy[wet]
        if wx.size > 20000:
            pick = np.linspace(0, wx.size - 1, 20000).astype(int)
            wx, wy = wx[pick], wy[pick]
        if "vel" in opened:
            _l, vsmp, _n = opened["vel"]
            v = vsmp.values_at(wx, wy) if _same_crs(opened, "vel") else None
            if v is not None:
                miss = float(np.mean(~np.isfinite(v)))
                vv = v[np.isfinite(v)]
                if miss > 0.05:
                    out.append((WARNING, "{}velocity is missing at {:.0%} of the wet cells - "
                                         "velocity 0 is used there".format(pre, miss)))
                if vv.size and float(vv.max()) > 50:
                    out.append((WARNING, "{}maximum velocity {:.0f} ft/s - check the units "
                                         "(the plugin expects ft/s)".format(pre,
                                                                           float(vv.max()))))
                elif vv.size:
                    out.append((OK, "{}velocity: max {:.1f} ft/s".format(pre, float(vv.max()))))
        if "arr" in opened:
            _l, asmp, _n = opened["arr"]
            a = asmp.values_at(wx, wy) if _same_crs(opened, "arr") else None
            if a is not None:
                aa = a[np.isfinite(a)]
                if aa.size:
                    amax = float(aa.max())
                    if amax > 500:
                        out.append((WARNING, "{}arrival times up to {:,.0f} - they look like "
                                             "minutes or seconds; the plugin expects HOURS"
                                    .format(pre, amax)))
                    elif amax <= 0:
                        out.append((WARNING, pre + "every arrival time is 0 - EAP ordering "
                                                   "and warning times will be meaningless"))
                    else:
                        out.append((OK, "{}arrival: {:.2f} - {:.1f} h".format(
                            pre, float(aa.min()), amax)))
                else:
                    out.append((WARNING, pre + "the arrival raster has no values where the "
                                               "depth raster is wet"))
        crss = {k: v[0].crs().authid() or v[0].crs().toWkt() for k, v in opened.items()
                if v[0].crs().isValid()}
        if len(set(crss.values())) > 1:
            out.append((WARNING, "{}rasters are in different coordinate systems ({}) - "
                                 "values are matched by location".format(
                                     pre, ", ".join("{} {}".format(opened[k][2], c)
                                                    for k, c in crss.items()))))
    finally:
        for _l, smp, _n in opened.values():
            smp.close()
    return out


def _same_crs(opened, key):
    try:
        return opened[key][0].crs() == opened["depth"][0].crs() or \
            not opened[key][0].crs().isValid()
    except Exception:
        return True


def check_services(urls, progress=None):
    """[(level, text)] for each (label, url) - an ArcGIS layer is asked for
    its description (?f=json), other addresses are fetched as they are."""
    from .download import http_get_bytes, offline_config
    from .constants import HTTP_USER_AGENT
    out = []
    # v2.6.1: offline data pack. v2.6.2: described from its manifest - a zip
    # pack is not unpacked here (Check inputs runs on the main thread; the
    # first unpack of a large pack would freeze QGIS)
    pack, only = offline_config()
    if pack:
        from .offline_pack import describe
        text = describe(pack)
        bad = any(w in text for w in ("Not found", "Not usable", "missing", "incomplete",
                                      "not an offline"))
        out.append((WARNING if bad else OK, "offline data pack {}: {}".format(
            os.path.basename(pack), text)))
    if only:
        out.append((OK, "offline only - the services are not contacted"))
        return out
    for i, (label, url) in enumerate(urls):
        if progress:
            progress(100.0 * i / max(1, len(urls)), "Checking {}...".format(label))
        probe = url.rstrip("/") + "?f=json" if ("Server/" in url or url.endswith("Server")) \
            else url
        try:
            raw = http_get_bytes(probe, user_agent=HTTP_USER_AGENT)
            txt = raw[:400].decode("utf-8", "replace").lower()
            if '"error"' in txt:
                out.append((WARNING, "{}: the service answers with an error - its address may "
                                     "have changed (Readme tab: Edit data sources)".format(label)))
            else:
                out.append((OK, "{}: reachable".format(label)))
        except Exception as e:
            out.append((WARNING, "{}: not reachable ({}) - that download will be skipped"
                        .format(label, str(e)[:80])))
    return out


def service_list(cfg):
    """(label, url) of the downloads a run with `cfg` would use."""
    from . import constants as C
    s = []
    if cfg.get("download_nsi", True):
        s.append(("NSI (USACE)", C.NSI_API_URL + "?bbox=-100.001,46.999,-100,47"))
    if cfg.get("download_address", True):
        s.append(("ND GIS Hub address points", C.ND_ADDRESS_POINTS_URL))
    if cfg.get("download_usa_structures", True):
        s.append(("FEMA USA Structures", C.USA_STRUCTURES_URL))
    if cfg.get("download_roads", True):
        s.append(("ND GIS Hub roads", C.ND_ROADS_URL))
        s.append(("FHWA HPMS (traffic)", C.HPMS_ND_URLS[0]))
    if cfg.get("download_rail", True):
        s.append(("Rail lines (NTAD)", C.NARN_URL))
        s.append(("Grade crossings (NTAD)", C.CROSSINGS_URL))
    if cfg.get("download_dams", True):
        s.append(("National Inventory of Dams", C.NID_URL))
    if cfg.get("download_lifelines", True):
        for _k, label, url, _c in C.lifeline_downloads():
            s.append((label, url))
    if cfg.get("download_crossings_structures", True):
        s.append(("National Bridge Inventory", C.NBI_URL))
    if cfg.get("download_lifelines", True) or cfg.get("download_crossings_structures", True):
        s.append(("OpenStreetMap Overpass", C.OVERPASS_URL.replace("interpreter", "status")))
    return s


def check_all(paths, output_folder="", dam_folder="", cfg=None, services=False,
              progress=None):
    """All checks: rasters of the breach (and no-breach) scenario, the
    building-outline file, optionally the services. [(level, text)]."""
    from .osm_processing import get_osm_candidates
    cfg = cfg or {}
    out = check_rasters(paths.get("depth"), paths.get("vel"), paths.get("arr"),
                        paths.get("dv"), label="Breach")
    if paths.get("base_depth"):
        out += check_rasters(paths.get("base_depth"), paths.get("base_vel"),
                             paths.get("base_arr"), label="No breach")
    if not paths.get("vel") and not cfg.get("one_d"):
        out.append((ERROR, "no velocity raster: ACER zones use max depth with max velocity "
                           "(Dmax / Vmax) - give the Vmax raster or choose the 1D "
                           "(depth-only) criterion"))
    if not paths.get("arr"):
        out.append((WARNING, "no arrival raster - no warning times, the EAP list is not "
                             "sorted by time"))
    if output_folder:
        try:
            outl = get_osm_candidates(output_folder, dam_folder)
        except Exception:
            outl = None
        if outl is not None:
            out.append((OK, "building outlines: {}".format(
                os.path.basename(outl.source().split("|")[0]) or outl.name())))
        elif cfg.get("download_usa_structures", True):
            out.append((OK, "no FEMA_Structures.gpkg yet - it will be downloaded"))
        else:
            out.append((WARNING, "no FEMA_Structures.gpkg and its download is off - "
                                 "structures come from points only"))
    if services:
        out += check_services(service_list(cfg), progress)
    return out


def summary(results):
    n_err = sum(1 for lvl, _t in results if lvl == ERROR)
    n_warn = sum(1 for lvl, _t in results if lvl == WARNING)
    return n_err, n_warn
