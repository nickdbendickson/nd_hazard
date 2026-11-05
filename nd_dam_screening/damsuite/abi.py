"""SARP / NACC National Aquatic Barrier Inventory (https://tool.aquaticbarriers.org) for the pack.

One definition for both suites (packbuild 1.1, suite 1.18.7): the layer names (ABI_*), where each
comes from and how its rows become points. DamSafety_Data's builder and the shared pack engine
(kind "abi") both use it, so the ND and DamSafety packs hold the same barrier layers.

- ABI_dams, ABI_small_barriers_surveyed: the public API, one state as CSV
  (<ABI_API>/<kind>/state?id=ND).
- ABI_road_crossings_unsurveyed: the national road-crossings download (unsurveyed crossings are not
  in the public API), its North Dakota rows with Surveyed = 'not likely'.

Every column is kept (names lower case, numbers as numbers), plus network (on-network /
off-network) and barrier_status (present / removed / excluded / removed+excluded; no row is
dropped). The old ND names SARP_dams / SARP_surveyed_crossings / SARP_unsurveyed_crossings answer
as aliases (hydro3dhp.BARRIER_ALIASES, part of hydro3dhp.ALIASES the pack readers use).

Pure Python: the download is passed in (download(url, dest, refresh, log, max_age) ->
(path, downloaded_now)).
"""
import csv
import datetime
import hashlib
import io
import json
import os
import re
import zipfile

from . import hydro3dhp as H

ABI_API = "https://tool.aquaticbarriers.org/api/v1/public"
ABI_ROAD_ZIP = "https://tool.aquaticbarriers.org/downloads/national/road_crossings.zip"
ABI_ROAD_MEMBER = "aquatic_barrier_ranks.csv"
ABI_STATE, ABI_STATE_NAME = "ND", "North Dakota"
ROAD_ZIP_MAX_AGE_S = 7 * 86400               # the national download is used again for a week

ABI = (
    ("ABI_dams", "SARP Aquatic Barrier Inventory - dams (on- and off-network)",
     {"mode": "api", "kind": "dams"}),
    ("ABI_small_barriers_surveyed", "SARP Aquatic Barrier Inventory - surveyed road / stream "
     "crossings (small barriers)", {"mode": "api", "kind": "barriers"}),
    ("ABI_road_crossings_unsurveyed", "SARP Aquatic Barrier Inventory - unsurveyed road / "
     "stream crossings", {"mode": "zip", "kind": "road_crossings"}),
)
NAMES = tuple(a[0] for a in ABI)
# the ND Dam Suite's names before the shared list (ND builder 1.3 - 1.4): old -> ABI_*
OLD_NAMES = {k: v for k, v in H.BARRIER_ALIASES.items() if k.startswith("SARP_")}

# every barrier is kept (removed / excluded ones too); barrier_status says which they are
ABI_NOTE = ("barrier_status = present / removed / excluded / removed+excluded (from the "
            "inventory's removed and excluded fields, both kept; removed: no longer a barrier, "
            "yearremoved gives the year; excluded: left out of SARP's network analysis). "
            "network = on-network / off-network.")


def source_of(spec):
    return "SARP / NACC National Aquatic Barrier Inventory ({})".format(
        "public API" if spec["mode"] == "api" else "national road-crossings download")


def url_of(spec):
    if spec["mode"] == "api":
        return "{}/{}/state?id={}".format(ABI_API, spec["kind"], ABI_STATE)
    return ABI_ROAD_ZIP


def read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _day(path):
    return datetime.date.fromtimestamp(os.path.getmtime(path)).isoformat()


def api_meta(kind, dest, refresh, log, download, errors=(OSError, ValueError)):
    """The public API's metadata (data_version, data_publish_date, citation, terms_of_use)."""
    try:
        p, _new = download("{}/{}/metadata".format(ABI_API, kind),
                           os.path.join(dest, "{}_metadata.json".format(kind)), refresh, log)
        m = read_json(p) or {}
    except tuple(errors) + (OSError, ValueError) as e:
        if log:
            log("  ABI metadata not read ({})".format(e))
        m = {}
    return {k: m.get(k) for k in ("data_version", "data_publish_date", "citation",
                                  "terms_of_use") if m.get(k)}


def readme_meta(text):
    out = {}
    for line in text.splitlines():
        if line.startswith("Data version:"):
            out["data_version"] = line.split(":", 1)[1].strip()
        elif line.startswith("Data published:"):
            out["data_publish_date"] = line.split(":", 1)[1].strip()
    return out


def abi_rows(spec, dest, download, refresh=False, log=None, errors=()):
    """(header, rows, provenance) of one inventory layer for North Dakota. spec: {"mode": "api"
    | "zip", "kind": ...}. api: <ABI_API>/<kind>/state?id=ND (CSV). zip: the national
    road-crossings download, its North Dakota rows kept (cached as a small CSV next to it) and
    only the unsurveyed ones (Surveyed = 'not likely'; 'yes' / 'likely' are the surveyed small
    barriers). errors: the caller's network errors (metadata that cannot be read is left out)."""
    kind = spec["kind"]
    if spec["mode"] == "api":
        url = url_of(spec)
        p, _new = download(url, os.path.join(dest, "{}_{}.csv".format(kind, ABI_STATE)),
                           refresh, log)
        with open(p, encoding="utf-8", newline="") as f:
            rd = csv.reader(f)
            head = [h.strip().lower() for h in next(rd)]
            rows = [r for r in rd if r]
        prov = {"source_url": url, "source_sha256": sha256(p), "source_date": _day(p),
                "filter": "state = {} (public API)".format(ABI_STATE)}
        prov.update(api_meta(kind, dest, refresh, log, download, errors))
        return head, rows, prov
    zp, new = download(ABI_ROAD_ZIP, os.path.join(dest, "road_crossings.zip"), refresh, log,
                       max_age=ROAD_ZIP_MAX_AGE_S)
    cache = os.path.join(dest, "road_crossings_{}.csv".format(ABI_STATE))
    with zipfile.ZipFile(zp) as z:
        if new or refresh or not os.path.isfile(cache) or \
                os.path.getmtime(cache) < os.path.getmtime(zp):
            with z.open(ABI_ROAD_MEMBER) as raw, open(cache + ".part", "w", encoding="utf-8",
                                                      newline="") as out:
                lines = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                wr = csv.writer(out)
                head = next(csv.reader([next(lines)]))
                wr.writerow(head)
                si = head.index("State")
                key = '"{}"'.format(ABI_STATE_NAME)
                # most of the 6 GB file is other states: a cheap text test first
                for line in lines:
                    if key in line:
                        r = next(csv.reader([line]))
                        if len(r) == len(head) and r[si] == ABI_STATE_NAME:
                            wr.writerow(r)
            os.replace(cache + ".part", cache)
        names = z.namelist()
        meta = readme_meta(z.read("README.txt").decode("utf-8", "replace")) \
            if "README.txt" in names else {}
        terms = z.read("TERMS_OF_USE.txt").decode("utf-8", "replace") \
            if "TERMS_OF_USE.txt" in names else ""
    with open(cache, encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        head = [h.strip().lower() for h in next(rd)]
        si = head.index("surveyed")
        rows = [r for r in rd if r and r[si] == "not likely"]
    prov = {"source_url": ABI_ROAD_ZIP, "source_sha256": sha256(zp),
            "source_file": "{} / {}".format(os.path.basename(zp), ABI_ROAD_MEMBER),
            "source_date": _day(zp),
            "filter": "State = {}, Surveyed = 'not likely' (unsurveyed)".format(ABI_STATE_NAME)}
    prov.update(meta)
    if terms:
        prov["terms_of_use"] = "By downloading " + terms.split("By downloading", 1)[-1]\
            .split("For your", 1)[0].strip()
        if "Citation:" in terms:
            prov["citation"] = terms.split("Citation:", 1)[1].strip("-\n ").strip()
    return head, rows, prov


_NUM = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?$")


def numeric_columns(head, rows):
    """Columns whose every value is a number (no leading zero, so HUC codes stay text)."""
    out = set()
    for i, _h in enumerate(head):
        vals = [r[i] for r in rows if i < len(r) and r[i] != ""]
        if vals and all(_NUM.match(v) for v in vals):
            out.add(i)
    return out


def _yes(v):
    return str(v).strip().lower() in ("yes", "true", "1", "y")


def barrier_status(props):
    """present / removed / excluded / removed+excluded from the inventory's removed and
    excluded fields (any case; missing = no)."""
    low = {k.lower(): v for k, v in props.items()}
    flags = [k for k in ("removed", "excluded") if _yes(low.get(k))]
    return "+".join(flags) if flags else "present"


def abi_features(head, rows, bbox):
    """Points (lon / lat) of the rows inside bbox, every column kept (numbers as numbers),
    plus network = on-network / off-network (from hasnetwork / onnetwork) and barrier_status
    (present / removed / excluded / removed+excluded; no row is dropped)."""
    la, lo = head.index("lat"), head.index("lon")
    nk = head.index("hasnetwork") if "hasnetwork" in head else \
        head.index("onnetwork") if "onnetwork" in head else -1
    nums = numeric_columns(head, rows)
    feats = []
    for r in rows:
        try:
            x, y = float(r[lo]), float(r[la])
        except (ValueError, IndexError):
            continue
        if not (bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]):
            continue
        props = {}
        for i, h in enumerate(head):
            if i in (la, lo) or i >= len(r):
                continue
            v = r[i]
            if i in nums and v != "":
                v = float(v) if any(c in v for c in ".eE") else int(v)
            elif v == "":
                v = None
            props[h] = v
        if nk >= 0:
            on = str(r[nk]).strip().lower() in ("yes", "true", "1")
            props["network"] = "on-network" if on else "off-network"
        props["barrier_status"] = barrier_status(props)
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [
            round(x, 6), round(y, 6)]}, "properties": props})
    return feats


def feature_uid(f):
    return "sarpid:{}".format(f["properties"].get("sarpid") or id(f))


def counts(feats):
    """{"network=on-network": n, "barrier_status=present": n, ...}"""
    out = {}
    for f in feats:
        p = f["properties"]
        for k in ("network", "barrier_status"):
            if k in p:
                key = "{}={}".format(k, p[k])
                out[key] = out.get(key, 0) + 1
    return out


NETWORK_FIELD = ("network: on-network / off-network (from the inventory's hasnetwork / "
                 "onnetwork); one layer, not two")
FLAG_FIELD = ("barrier_status: present / removed / excluded / removed+excluded (removed and "
              "excluded barriers are kept, not dropped)")
