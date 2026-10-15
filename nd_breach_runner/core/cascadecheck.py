"""Does this dam's flood reach another dam? (3.20.0)

Feature 4 of the review. The chain tool (core/cascade.py) already models a chain of dams the
engineer has named, upstream first. What nobody did was ASK the question after an ordinary
single-dam run: is there a dam inside the flood we just computed? A reviewer asks it first, and
a single-dam study cannot answer it from inside.

The test is damsuite.cascade (point in polygon, great-circle miles, shared with the other two
plugins). This module supplies the two halves it needs from the Runner's own output - the flood
footprint and the dam inventory - and writes the hand-off for running the dam below next:

    info = check(folder, dams_folder, dam_id, to_lonlat=...)
    info["text"]                        # damsuite.cascade.summary_text
    paths = write_jobs(folder, info, dams_folder, hydrograph=res.hydrograph_csv)

The footprint is the run's own "Flood Extent.geojson", whose coordinates are in the model CRS,
so the caller passes `to_lonlat(x, y)` (QGIS's transform; the window and the tools have one).
The inventory is every dam that has a folder under the Dams folder, read from its dam.json
(lon / lat, name, existing class) - pure Python, no layer needed - plus any dams the caller
read from an inventory layer.

HOW FAR THIS GOES, AND WHERE IT STOPS
The job that is written records the upstream run's breach outflow hydrograph as the downstream
dam's inflow, and copies it into that dam's folder as inputs/upstream_breach_inflow.csv. It is
NOT consumed automatically, and nothing in this release runs the second dam by itself. Two
reasons, both about not changing a number by accident:

  * the Runner's flood scenario reads inputs/pmf_inflow.csv as THE reservoir inflow. Writing
    the upstream wave there would replace the downstream dam's own PMF - and if that PMF is
    the bigger flow, the run that followed would be less severe than the one the engineer
    asked for. This suite does not move a result in that direction silently.
  * the physically right answer is the two together. So when the downstream dam already has a
    pmf_inflow.csv, `write_jobs` also writes inputs/pmf_inflow_plus_upstream.csv - the sum of
    the two hydrographs on the union of their time steps - and the job names it. The engineer
    points the run's INFLOW_CSV at whichever file they mean. A sum is the larger flow, so
    choosing it never makes the run milder.

The report says in plain words that a downstream dam was found, that the inflow is recorded,
and that the chain itself has to be run - with 'Cascade failure of dams in a chain' or by
pointing the next run at the file.

Pure Python + the vendored damsuite; no QGIS, no numpy.
"""
import json
import os

from ..damsuite import cascade as CAS

JOB_FOLDER = "_cascade"
JOB_NAME = "next_jobs.json"
INFLOW_NAME = "upstream_breach_inflow.csv"
SUM_NAME = "pmf_inflow_plus_upstream.csv"
INPUTS_DIR = "inputs"
PMF_NAME = "pmf_inflow.csv"
EXTENT_NAME = "Flood Extent.geojson"
NOT_CONSUMED = ("The inflow is RECORDED, not used: this release does not feed it to the next "
                "run by itself. Run the chain with 'Cascade failure of dams in a chain', or "
                "point the next run's PMF inflow at the file named here.")


def extent_path(folder):
    return os.path.join(folder or "", EXTENT_NAME)


def rings_from_extent(path, to_lonlat=None):
    """(rings, problem): the flood footprint as [[(lon, lat), ...], ...].

    `to_lonlat(x, y)` converts the model CRS to WGS 84; without it the coordinates are taken
    as lon / lat already (a flood extent written in EPSG:4326)."""
    if not path or not os.path.isfile(path):
        return [], "no flood extent at {}".format(path or "(none)")
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError) as e:
        return [], "{} could not be read ({})".format(os.path.basename(path), e)
    rings = CAS.rings_from_geojson(obj)
    if not rings:
        return [], "{} holds no polygon".format(os.path.basename(path))
    if to_lonlat is None:
        return rings, ""
    out = []
    for ring in rings:
        got = []
        for x, y in ring:
            try:
                lon, lat = to_lonlat(x, y)
            except Exception:                                    # noqa - one bad vertex
                continue
            if lon is None or lat is None:
                continue
            got.append((float(lon), float(lat)))
        if len(got) >= 3:
            out.append(got)
    if not out:
        return [], "the flood extent could not be put into longitude / latitude"
    return out, ""


def dams_from_folders(dams_folder, skip_id=""):
    """[{"dam_id", "name", "lon", "lat", "existing_class", "folder"}] for every dam folder that
    records a point in its dam.json. The cheapest inventory there is, and it is exactly the
    dams this user works on."""
    out = []
    if not dams_folder or not os.path.isdir(dams_folder):
        return out
    try:
        from ..damsuite import dam_record as DR
    except Exception:                                            # noqa
        return out
    skip = str(skip_id or "").strip().casefold()
    try:
        names = sorted(os.listdir(dams_folder))
    except OSError:
        return out
    for n in names:
        p = os.path.join(dams_folder, n)
        if not os.path.isdir(p):
            continue
        try:
            rec = DR.load(p)
        except Exception:                                        # noqa - a broken dam.json
            continue
        if rec is None:
            continue
        lon, lat = rec.get("lon"), rec.get("lat")
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
            continue
        did = str(rec.get("dam_id") or n).strip()
        if did.casefold() == skip or n.casefold() == skip:
            continue
        out.append({"dam_id": did, "name": str(rec.get("name") or ""), "lon": float(lon),
                    "lat": float(lat),
                    "existing_class": str(rec.get("existing_hazard_class") or ""),
                    "folder": p})
    return out


def check(folder, dams_folder, dam_id="", source=None, to_lonlat=None, dams=None,
          extent=""):
    """Which inventory dams lie inside this run's flood.

    folder       the dam folder of the run that has just finished
    source       (lon, lat) of this dam, for the distances ("" / None: no distances)
    dams         extra dam dicts (an inventory layer the caller read); the dam folders of
                 `dams_folder` are always included

    Returns {"hits", "text", "rings", "dams_checked", "problem", "source", "source_id"}."""
    rings, problem = rings_from_extent(extent or extent_path(folder), to_lonlat)
    inventory = list(dams or []) + dams_from_folders(dams_folder, dam_id)
    hits = CAS.downstream(inventory, rings, source=source, source_id=dam_id) if rings else []
    return {"hits": hits, "rings": rings, "problem": problem,
            "dams_checked": len(inventory), "source": source, "source_id": dam_id,
            "text": (problem + " The cascade check did not run."
                     if problem else CAS.summary_text(hits))}


def jobs(info, dams_folder, hydrograph=""):
    """The hand-off dicts for the dams found (damsuite.cascade.next_job)."""
    return CAS.jobs((info or {}).get("hits"), dams_folder, hydrograph,
                    (info or {}).get("source_id") or "")


# ------------------------------------------------------------- the hydrographs --
def read_hydrograph(path):
    """[(time_hr, discharge_cfs)] from a two-column CSV (the Runner's own format)."""
    rows = []
    with open(path, encoding="utf-8-sig") as fh:
        for i, line in enumerate(fh):
            parts = [p.strip() for p in line.replace("\t", ",").split(",")]
            if len(parts) < 2:
                continue
            try:
                rows.append((float(parts[0]), float(parts[1])))
            except ValueError:
                if i == 0:
                    continue                 # the header
    return rows


def _at(rows, t):
    """Linear interpolation; zero outside the record (a hydrograph that has not started or has
    finished carries no flow)."""
    if not rows:
        return 0.0
    if t <= rows[0][0]:
        return rows[0][1] if t == rows[0][0] else 0.0
    if t >= rows[-1][0]:
        return rows[-1][1] if t == rows[-1][0] else 0.0
    for (t0, q0), (t1, q1) in zip(rows[:-1], rows[1:]):
        if t0 <= t <= t1:
            if t1 == t0:
                return max(q0, q1)
            return q0 + (q1 - q0) * (t - t0) / (t1 - t0)
    return 0.0                                                   # pragma: no cover


def add_hydrographs(a, b):
    """The sum of two hydrographs on the union of their times, [(time_hr, cfs)].

    Both are taken as starting at the same instant - the upstream dam's breach initiation -
    which is the conservative reading: a wave that in truth arrives later would be added to a
    lower part of the downstream dam's own flood."""
    times = sorted({round(t, 6) for t, _q in list(a or []) + list(b or [])})
    return [(t, _at(a, t) + _at(b, t)) for t in times]


def write_hydrograph(path, rows, header="time_hr,discharge_cfs"):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(header + "\n")
        for t, q in rows or []:
            fh.write("{:.5f},{:.3f}\n".format(float(t), float(q)))
    os.replace(tmp, path)
    return path


def write_jobs(folder, info, dams_folder, hydrograph="", version=""):
    """Write the cascade hand-off: <dam folder>/_cascade/next_jobs.json, and for each dam found
    a copy of this run's breach outflow in its own inputs/ folder.

    Returns {"path", "jobs", "notes"} - `notes` are the log lines, including the plain statement
    that the inflow is not consumed automatically."""
    out = {"path": "", "jobs": [], "notes": []}
    hits = (info or {}).get("hits") or []
    got = jobs(info, dams_folder, hydrograph)
    rows = []
    if hydrograph and os.path.isfile(hydrograph):
        try:
            rows = read_hydrograph(hydrograph)
        except (OSError, ValueError) as e:
            out["notes"].append("the breach outflow hydrograph could not be read ({}); the job "
                                "records its path only".format(e))
    for job in got:
        job["inflow_role"] = CAS.ROLE_INFLOW
        job["consumed"] = False
        job["note"] = (job.get("note") or "") + " " + NOT_CONSUMED
        dam_folder = job.get("folder") or ""
        if not dam_folder or not rows:
            continue
        inputs = os.path.join(dam_folder, INPUTS_DIR)
        try:
            dst = write_hydrograph(os.path.join(inputs, INFLOW_NAME), rows)
            job["inflow_copy"] = dst
            out["notes"].append("{}: this run's breach outflow written as {}".format(
                job.get("dam_id") or "?", dst))
            pmf = os.path.join(inputs, PMF_NAME)
            if os.path.isfile(pmf):
                total = add_hydrographs(read_hydrograph(pmf), rows)
                s = write_hydrograph(os.path.join(inputs, SUM_NAME), total)
                job["inflow_plus_own"] = s
                out["notes"].append(
                    "{}: it already has {}, so the sum of the two was written as {} (the sum "
                    "is the larger flow; the run uses whichever file you name)".format(
                        job.get("dam_id") or "?", PMF_NAME, os.path.basename(s)))
        except (OSError, ValueError) as e:
            out["notes"].append("{}: the inflow file was not written ({})".format(
                job.get("dam_id") or "?", e))
    out["jobs"] = got
    if not hits:
        return out
    d = os.path.join(folder, JOB_FOLDER)
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, JOB_NAME)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"tool": "ndbreach", "version": version, "from_dam": info.get("source_id"),
                       "hydrograph": hydrograph, "inflow_consumed": False,
                       "note": NOT_CONSUMED, "hits": hits, "jobs": got}, fh, indent=1)
        out["path"] = p
        out["notes"].append("Cascade hand-off written: {}".format(p))
    except OSError as e:
        out["notes"].append("the cascade hand-off was not written ({})".format(e))
    return out


def report_lines(info, written=None, title="Cascade: does the flood reach another dam?"):
    """The run report section."""
    if not info:
        return []
    lines = ["## " + title, "", "- " + (info.get("text") or "")]
    lines.append("- {} dam(s) of the inventory were tested.".format(info.get("dams_checked")
                                                                    or 0))
    for h in (info.get("hits") or [])[:12]:
        lines.append("  - {}{}{}{}".format(
            h.get("dam_id") or h.get("name") or "?",
            " " + h["name"] if h.get("dam_id") and h.get("name") else "",
            " at {:.1f} mi".format(h["distance_mi"]) if h.get("distance_mi") is not None
            else "",
            "; class on record {}".format(h["existing_class"]) if h.get("existing_class")
            else ""))
    if info.get("hits"):
        lines.append("- " + NOT_CONSUMED)
    for n in (written or {}).get("notes") or []:
        lines.append("- " + n)
    lines.append("")
    return lines
