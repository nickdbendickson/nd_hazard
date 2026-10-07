# -*- coding: utf-8 -*-
"""v2.8: the optional analyses that follow the classification, called from
pipeline._classify_and_write. Each step is optional (a cfg switch), never fails
the run (a problem is logged and shown in the summary), and returns the lines
for its summary section. Kept out of pipeline.py so that file stays readable."""

import csv
import json
import os
import time

from .helpers import RasterSampler, prepare_output_path
from .scenarios import restore


def _relpath(run, path):
    try:
        return os.path.relpath(path, run.out)
    except ValueError:
        return path


def parse_factors(text):
    """[floats] from "0.8, 0.9 1.1" (or a list); bad entries are dropped."""
    if text is None:
        return None
    if isinstance(text, (list, tuple)):
        items = text
    else:
        items = str(text).replace(";", " ").replace(",", " ").split()
    out = []
    for x in items:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            continue
    return out or None


def _open_depth(layers, gov):
    lyr = layers[gov.name].get("depth")
    return RasterSampler(lyr) if lyr is not None else None


def _guard(run, label, fn):
    """(lines) of fn(); a cancel goes on, any other failure becomes a log
    line and a summary line."""
    from .helpers import CancelledByUser
    try:
        return fn() or []
    except CancelledByUser:
        raise
    except Exception as e:                       # never fails the run
        run.log(">>> WARNING: {} failed: {}".format(label, e))
        return ["{} not made: {}".format(label, e)]


def _retire(run, d_qa, csv_bases=(), layer_bases=()):
    """Remove the files a step wrote on an EARLIER run before the step runs (or is skipped),
    so a table of an old run never sits next to this run's results (the rule of the rest
    of the pipeline: never leave a previous run's result)."""
    try:
        from .pipeline import _retire_output, _retire_everywhere
        for b in csv_bases:
            _retire_output(d_qa, b, ("csv",))
        for b in layer_bases:
            _retire_everywhere(run.out, b)
    except Exception as e:                                   # never fails the run
        run.log(">>> NOTE: old result files of an earlier run were not all removed: {}".format(e))


# ------------------------------------------------------------ sensitivity --
def sensitivity_step(run, gov, governing, structs, base_snaps, layers, inputs, one_d, judge,
                     d_qa, written):
    cfg = run.cfg
    _retire(run, d_qa, ("Sensitivity", "Sensitivity_Structures"))
    if not cfg.get("sensitivity"):
        return []

    def go():
        from . import sensitivity as SE
        run.tick(89, "Sensitivity table...")
        restore(structs, governing["snap"])
        base = base_snaps.get(gov.baseline) if gov.baseline else None
        same = True
        if base is not None:
            same = (layers[gov.name].get("vel") is not None) == \
                (layers[base[1].name].get("vel") is not None)
        try:
            res = SE.run(
                structs, governing.get("roads"), governing.get("rails"),
                factors=parse_factors(cfg.get("sensitivity_factors")) or SE.DEFAULT_FACTORS,
                split=bool(cfg.get("sensitivity_split")),
                cfg=cfg, res=governing, one_d=one_d, judgment_as_probable=judge,
                same_method=same, base_snap=base[0] if base else None,
                incremental=bool(governing.get("incremental")),
                roads_evaluated=inputs["roads_on"], rail_evaluated=inputs["rail_on"],
                structs_evaluated=inputs.get("structs_on", True),
                nd_base=governing["nd"], tm11_base=governing["tm11"])
        finally:
            restore(structs, governing["snap"])          # what it scaled is put back
        governing["sensitivity"] = res
        if res.get("available"):
            for p in SE.write_csvs(d_qa, res) or []:
                written.append(_relpath(run, p))
        if res.get("verdict"):
            run.log(">>> Sensitivity: " + res["verdict"])
        return SE.summary_lines(res)
    return _guard(run, "Sensitivity table", go)


# ------------------------------------------------------------ termination --
def termination_step(run, gov, governing, layers, inputs, clip, dam_name, d_qa, written,
                     export_show):
    cfg = run.cfg
    _retire(run, d_qa, ("Termination_Point", "Termination_Stations"), ("EAP_Termination_Point",))
    if not cfg.get("termination"):
        return []

    def go():
        from . import termination as TM
        from qgis.core import QgsVectorLayer
        from .constants import GRP_EAP, TRAVEL_STATION_MI
        run.tick(90, "Downstream termination point...")
        depth = _open_depth(layers, gov)
        arr = RasterSampler(layers[gov.name]["arr"]) if layers[gov.name].get("arr") is not None \
            else None
        base = None
        if gov.baseline and gov.baseline in layers and layers[gov.baseline].get("depth") is not None:
            base = RasterSampler(layers[gov.baseline]["depth"])
        try:
            dam_pt = None
            if run.subject_dam is not None:
                try:
                    p = run.subject_dam["geom"].asPoint()
                    dam_pt = (p.x(), p.y())
                except Exception:
                    dam_pt = None
            flow = None
            if inputs.get("flowlines"):
                flow = QgsVectorLayer(inputs["flowlines"], "NHD flowlines", "ogr")
                if not flow.isValid():
                    flow = None
            spacing = float(cfg.get("travel_station_mi") or TRAVEL_STATION_MI)
            line, notes = TM.trace_line(depth, arr, clip.crs, run.ctx, clip.lat,
                                        centerline_layer=cfg.get("centerline_layer"),
                                        flowline_layer=flow, dam_point=dam_pt,
                                        spacing_mi=spacing, clip=clip)
            if not line:
                msg = "; ".join(notes) or "no centerline"
                if cfg.get("centerline_layer") is None and not inputs.get("flowlines"):
                    msg = "no centerline layer given and no NHD flowlines downloaded"
                run.log(">>> Termination point not made: " + msg)
                return ["Termination point not made: " + msg]
            res = TM.analyze(line, depth, clip.crs, run.ctx, clip.lat, base_sampler=base,
                             rise_ft=float(cfg.get("term_rise_ft", 2.0)),
                             depth_ft=float(cfg.get("term_depth_ft", 2.0)),
                             confirm=int(cfg.get("term_confirm", 3)), spacing_mi=spacing)
        finally:
            for s in (depth, arr, base):
                if s is not None:
                    s.close()
        governing["termination"] = res
        for p in (TM.write_csv(d_qa, res), TM.write_stations_csv(d_qa, res)):
            if p:
                written.append(_relpath(run, p))
        lyr = TM.to_layer(res, clip.crs)
        if lyr is not None:
            export_show(run, lyr, d_qa, "EAP_Termination_Point", ("gpkg",), dam_name, GRP_EAP,
                        written, "Status")
        for w in res.get("warnings") or []:
            run.log(">>> WARNING: termination point: " + w)
        return TM.summary_lines(res)
    return _guard(run, "Termination point", go)


# ------------------------------------------------------ population source --
def popsource_step(run, gov, governing, layers, d_qa, written):
    cfg = run.cfg
    src = cfg.get("pop_source")
    _retire(run, d_qa, ("Population_Source_CrossCheck", "Population_Source_Polygons"))
    if src is None:
        return []

    def go():
        from . import popsource as PS
        run.tick(90, "Second population source...")
        depth = _open_depth(layers, gov)
        try:
            lines, paths, res = PS.run_check(
                src, depth, run.ctx, governing.get("conseq") or {}, d_qa,
                population=cfg.get("population", "night"), log=run.log,
                **PS.options_from_cfg(cfg))
        finally:
            if depth is not None:
                depth.close()
        for p in paths:
            written.append(_relpath(run, p))
        if res is not None and res.get("ok"):
            run.sources.append("Second population source: {} ({})".format(
                res.get("layer"), res.get("kind")))
        governing["popsource"] = res
        return lines
    return _guard(run, "Second population source", go)


# --------------------------------------------------------------- evacuation --
def evacuation_step(run, gov, governing, structs, layers, inputs, d_qa, written,
                    d_eap=None, export_show=None, dam_name=""):
    cfg = run.cfg
    _retire(run, d_qa, ("Evacuation_Timing",))
    _retire(run, d_qa, (), ("EAP_Evacuation_Routes", "EAP_Road_Closures"))
    if not cfg.get("evacuation"):
        return []

    def go():
        from . import evacuation as EV
        from qgis.core import QgsVectorLayer
        run.tick(90, "Evacuation timing...")
        roads = None
        src = inputs.get("roads_source")
        if src and src[0] and src[1] and src[1] != "memory":
            # the layer as the project has it (GeoPackage table, subset filter kept)
            roads = QgsVectorLayer(src[0], "roads", src[1])
        if roads is None or not roads.isValid():
            path = inputs.get("roads_path")
            roads = QgsVectorLayer(path, "roads", "ogr") if path else None
        if roads is None or not roads.isValid():
            run.log(">>> Evacuation timing not made: no road layer file (roads are needed - "
                    "download them or give a road layer saved as a file)")
            return ["Evacuation timing not made: no road layer file"]
        restore(structs, governing["snap"])
        depth = _open_depth(layers, gov)
        arr = RasterSampler(layers[gov.name]["arr"]) if layers[gov.name].get("arr") is not None \
            else None
        try:
            lines, paths, res = EV.run_evacuation(
                roads, structs, depth, arr, run.ctx, d_qa, log=run.log, lat=run.clip_lat,
                **EV.options_from_cfg(cfg))
        finally:
            for s in (depth, arr):
                if s is not None:
                    s.close()
        for p in paths:
            written.append(_relpath(run, p))
        # v2.9: the routes and the road pieces that close, as map layers (EAP group)
        keep = bool(cfg.get("evac_routes", True))
        routes = closures = None
        if keep:
            routes, closures = EV.route_layers(res, crs=run.clip_crs)
        else:
            EV.release_routes(res)
        if export_show is not None and (routes is not None or closures is not None):
            from .constants import GRP_EAP
            for lyr, base, field in ((routes, EV.ROUTES_LAYER, "Status"),
                                     (closures, EV.CLOSURES_LAYER, None)):
                if lyr is None or lyr.featureCount() == 0:
                    continue
                export_show(run, lyr, d_eap or d_qa, base, ("gpkg", "csv"), dam_name, GRP_EAP,
                            written, field, False)
            line = EV.route_summary_line(routes, closures)
            if line:
                lines = list(lines) + [line]
        governing["evacuation"] = res
        return lines
    return _guard(run, "Evacuation timing", go)


# ------------------------------------------------ v2.9: consequence sensitivity --
def conseq_sensitivity_step(run, gov, governing, structs, d_qa, written):
    """Loss-of-life / population-at-risk range for other warning times, persons per
    dwelling, time of day and severity method (QA/Sensitivity_Consequences.csv). It only
    reads the records; nothing about the classification changes."""
    cfg = run.cfg
    _retire(run, d_qa, ("Sensitivity_Consequences",))
    if not cfg.get("conseq_sensitivity") or not cfg.get("lol", True):
        return []

    def go():
        from . import sensitivity as SE
        run.tick(89, "Consequence sensitivity...")
        restore(structs, governing["snap"])
        try:
            res = SE.run_consequences(structs, cfg, governing.get("conseq"),
                                      incremental=bool(governing.get("incremental")),
                                      **SE.conseq_options_from_cfg(cfg))
        finally:
            restore(structs, governing["snap"])
        governing["sensitivity_conseq"] = res
        if res.get("available"):
            for p in SE.write_conseq_csv(d_qa, res) or []:
                written.append(_relpath(run, p))
        if res.get("verdict"):
            run.log(">>> Consequence sensitivity: " + res["verdict"])
        return SE.summary_lines(res)
    return _guard(run, "Consequence sensitivity", go)


# ------------------------------------------------------ v2.9: KMZ / GeoJSON export --
def webexport_step(run, dam_name, d_eap, written):
    """KMZ + GeoJSON copies of the EAP layers in EAP/Web (main thread, after the map book)."""
    cfg = run.cfg
    if not cfg.get("web_export") or not cfg.get("eap"):
        _retire_web(run, d_eap)
        return []

    def go():
        from . import webexport as WX
        run.tick(96, "KMZ / GeoJSON export...")
        res = WX.export_eap_web(d_eap, dam_name, log=run.log)
        for name, r in res.items():
            for k in ("kmz", "geojson"):
                if isinstance(r, dict) and r.get(k):
                    written.append(_relpath(run, r[k]))
        return WX.summary_lines(res)
    return _guard(run, "KMZ / GeoJSON export", go)


def _retire_web(run, d_eap):
    """The Web copies of an earlier run are removed when the option is off."""
    try:
        import shutil
        folder = os.path.join(d_eap, "Web")
        if os.path.isdir(folder) and not run.cfg.get("web_export"):
            shutil.rmtree(folder, ignore_errors=True)
    except Exception:                                      # noqa: BLE001
        pass


# ------------------------------------------------------ v2.9: Temporal Controller --
def temporal_step(run, gov, suffix, d_eap):
    """Set the arrival layers of the project up for the Temporal Controller (animation of
    the flood arrival). Main thread; the layers must already be in the project."""
    cfg = run.cfg
    if not cfg.get("temporal_animation") or not cfg.get("add_to_project"):
        return []
    from . import temporal as TP
    from .extras290_tab import parse_start_time
    items = []
    for base, field, units in TP.ANIMATED_LAYERS:
        cand = []
        for sfx in (suffix, ""):
            pth = os.path.join(d_eap, base + sfx + ".gpkg")
            cand.append(os.path.normcase(os.path.abspath(pth)))
        lyr = next((run.added.get(c) for c in cand if run.added.get(c) is not None), None)
        if lyr is not None:
            items.append((lyr, field, units))
    if not items:
        return ["Temporal animation not set up: no EAP arrival layer in the project"]
    start = parse_start_time(cfg.get("temporal_breach_start"))
    try:
        step = float(cfg.get("temporal_step_min") or TP.DEFAULT_STEP_MIN)
    except (TypeError, ValueError):
        step = TP.DEFAULT_STEP_MIN
    from qgis.core import QgsProject
    n, msgs = TP.animate_layers(items, breach_start=start, step_minutes=step,
                                project=QgsProject.instance())
    for m in msgs:
        run.log(">>> Temporal animation: {}".format(m))
    if not n:
        return ["Temporal animation not set up: " + "; ".join(msgs)]
    return ["Temporal Controller: {} layer(s) animated from the breach start ({}), step {:g} "
            "min - the start is a nominal display time".format(
                n, start.strftime("%Y-%m-%d %H:%M") if start else "2000-01-01 00:00", step)]


# ------------------------------------------------------------------ vintage --
_NID_LOCATION_SOURCE = "NID subject dam"     # written by location_file_step
VINTAGE_CSV = "Data_Vintage.csv"
VINTAGE_HEAD = ["Source", "Detail", "Date", "Note"]


def download_vintage_snapshot():
    """What the downloads of THIS run's stage A rest on: the offline-pack data sets, the
    earlier-day copies and the tile-cache vintage. Kept in the stage-A state, so a re-used
    evaluation (memory or disk) reports its own downloads, not those of whatever dam
    was evaluated last."""
    try:
        from .download import PACK_USED, STALE_USED, tile_vintage
        return {"pack": [list(t) for t in PACK_USED], "stale": [list(t) for t in STALE_USED],
                "tiles": tile_vintage()}
    except Exception:
        return {"pack": [], "stale": [], "tiles": []}


def vintage_rows(run, layers, snap=None):
    """[(source, detail, date, note)] of what this run's results rest on. snap:
    download_vintage_snapshot() of the evaluation (None: the live records)."""
    from .constants import PLUGIN_VERSION
    rows = [("Plugin", "ND Dam Hazard Classification Helper v{}".format(PLUGIN_VERSION),
             time.strftime("%Y-%m-%d"), "this run")]
    for name, lyrs in layers.items():
        for kind in ("depth", "vel", "arr", "dv"):
            lyr = lyrs.get(kind)
            src = lyr.source() if lyr is not None else ""
            if src and os.path.isfile(src.split("|")[0]):
                p = src.split("|")[0]
                rows.append(("Raster", "{} - {} ({})".format(name, kind, os.path.basename(p)),
                             time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(p))),
                             "file modified"))
    try:
        if snap is None:
            snap = download_vintage_snapshot()
        for label, _key, fetched, n, partial in snap.get("pack") or []:
            rows.append(("Offline data pack", "{}: {} feature(s)".format(label, n), fetched or "",
                         "PARTIAL for the study area" if partial else "from the pack"))
        for v in snap.get("tiles") or []:
            rows.append(("Tile cache", "{}: {} tile(s) ({} cached, {} downloaded now)".format(
                v["source"], v["tiles"], v["from_cache"], v["downloaded"]),
                "{} to {}".format(v["oldest_fetched"], v["newest_fetched"]),
                "{} STALE (the service did not answer)".format(v["stale"]) if v["stale"]
                else "fresh"))
        for name, day, fname in snap.get("stale") or []:
            rows.append(("Earlier download", "{} ({})".format(name, fname), day,
                         "the service did not answer - an earlier download was used"))
    except Exception:
        pass
    return rows


def vintage_step(run, layers, d_qa, written, snap=None):
    def go():
        rows = vintage_rows(run, layers, snap)
        path = prepare_output_path(os.path.join(d_qa, VINTAGE_CSV))
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(VINTAGE_HEAD)
            w.writerows(rows)
        written.append(_relpath(run, path))
        lines = ["{}: {} - {} ({})".format(s, d, dt, n) if n else "{}: {} - {}".format(s, d, dt)
                 for s, d, dt, n in rows]
        lines.append("Downloaded services (NSI, ND GIS Hub, FEMA, NID ...) report no data "
                     "version: the date fetched is the vintage. Table: QA/{}".format(VINTAGE_CSV))
        return lines
    return _guard(run, "Data-vintage table", go)


# ------------------------------------------------------ dam location (series) --
def location_file_step(run, acrs):
    """Leave dam_location.json in the dam folder (the NID subject dam's point,
    WGS 84) so 'Dams in series' can find this dam without typing."""
    subj = run.subject_dam
    dam_folder = run.cfg.get("dam_folder") or ""
    if subj is None or not dam_folder or not os.path.isdir(dam_folder):
        return
    try:
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY
        from . import series as SR
        # never replace the engineer's own location (a file they wrote or edited): only
        # a file this step wrote earlier is refreshed. A dam_location.csv of theirs
        # would lose to a new .json, so it is left alone too.
        js = os.path.join(dam_folder, SR.LOCATION_JSON)
        if os.path.isfile(js):
            try:
                with open(js, "r", encoding="utf-8") as fh:
                    src = str((json.load(fh) or {}).get("source") or "")
            except (OSError, ValueError, AttributeError):
                src = ""
            if not src.startswith(_NID_LOCATION_SOURCE):
                return
        elif os.path.isfile(os.path.join(dam_folder, SR.LOCATION_CSV)):
            return
        p = subj["geom"].asPoint()
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        q = QgsCoordinateTransform(acrs, wgs, run.ctx).transform(QgsPointXY(p.x(), p.y()))
        SR.write_location_file(dam_folder, lon=q.x(), lat=q.y(), nid=subj.get("nid") or "",
                               name=subj.get("name") or "", source=_NID_LOCATION_SOURCE + " (v2.8 run)")
    except Exception as e:
        run.log(">>> NOTE: dam_location.json not written: {}".format(e))
