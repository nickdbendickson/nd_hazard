"""Are the services up, asked before a 90-minute run starts (ND Data Updater 1.4.0).

The expensive failure is not a service being down - it is finding out two hours in. The USGS
3DHP service in particular answers a small question and then returns HTTP 504 to a state-sized
one for half an hour, and a user who knew that beforehand would have chosen the National Map
file instead. So this module asks every service one small, cheap question with a short timeout
and reports reachable / slow / down, on demand or when the pack window opens.

    rows = check()                        # [{"key","label","state","seconds","detail",...}]
    print(text_report(rows))
    warning_text(rows)                    # "" when nothing needs saying

Decisions:
  * Each probe is a metadata request ("?f=json" on an ArcGIS layer, a one-byte GET elsewhere),
    never a data query: the point is "does it answer", and a real query is what we are trying to
    avoid paying for.
  * Hard limits: a 6-second timeout, at most 64 KB read, and all probes in parallel, so the whole
    check is over in about one timeout. "slow" is the useful middle answer - above 2.5 seconds
    for a metadata request means the service is loaded and a state-sized query will struggle.
  * 3DHP gets a second probe with the state-sized bounding box (count only), because that is the
    request that fails while the small one succeeds. It is the only probe allowed to be slow
    without being called slow: `blocking` names it as the one that decides whether a pack build
    should use the National Map file instead.
  * The opener and the clock are arguments, so the logic is tested without a network.
  * Nothing here raises: a probe that fails in any way is "down" with the reason as its detail.
Pure Python standard library; no QGIS (the plugin runs it on a worker thread).
"""

import json
import time
import urllib.error
import urllib.request

TIMEOUT = 6.0
SLOW_AFTER = 2.5
READ_LIMIT = 64 * 1024
USER_AGENT = "ND-Data-Updater-QGIS/1.4.0"
STATE_BBOX = "-104.1,45.9,-96.5,49.1"
_3DHP = "https://hydro.nationalmap.gov/arcgis/rest/services/3DHP_all/MapServer/50"
_COUNT = ("/query?where=1%3D1&returnCountOnly=true&f=json&inSR=4326&geometryType="
          "esriGeometryEnvelope&spatialRel=esriSpatialRelIntersects&geometry=")

# (key, label, url, kind, what it is for)
SERVICES = (
    ("nsi", "USACE National Structure Inventory (NSI)",
     "https://nsi.sec.usace.army.mil/nsiapi/structures?fips=38015&fmt=fc", "head",
     "the structures every hazard class is counted from"),
    ("3dhp", "USGS 3DHP hydrography (small question)", _3DHP + "?f=json", "json",
     "the streams and lakes the breach is routed down"),
    ("3dhp_state", "USGS 3DHP hydrography (statewide question)",
     _3DHP + _COUNT + STATE_BBOX, "json",
     "the request a pack build really makes; when this is down, use the National Map file"),
    ("nid", "National Inventory of Dams (NID)",
     "https://geospatial.sec.usace.army.mil/dls/rest/services/NID/NID_public/FeatureServer/0"
     "?f=json", "json", "the dam inventory"),
    ("fema", "FEMA / ORNL USA Structures",
     "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
     "USA_Structures_View/FeatureServer/0?f=json", "json",
     "the building outlines with occupancy class"),
    ("dwr", "ND DWR dams (ND GIS Hub)",
     "https://ndgishub.nd.gov/arcgis/rest/services/Applications/DWR_Dams/MapServer?f=json",
     "json", "the DWR dams inventory when no DWR export is in the data folder"),
    ("mrlc", "MRLC land cover (NLCD)",
     "https://www.mrlc.gov/geoserver/mrlc_download/wcs?service=WCS&version=2.0.1"
     "&request=GetCapabilities", "head", "the land cover Manning's n comes from"),
)
# the probe whose failure should change what a run does
DECIDES = ("3dhp_state", "3dhp")
STATES = ("up", "slow", "down")


def _default_opener(url, timeout):
    from .ndpack.damsuite import netproxy            # QGIS's proxy setting applies (1.4.2)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return netproxy.urlopen(req, timeout=timeout)


def classify(ok, seconds, detail="", slow_after=SLOW_AFTER):
    """("up" | "slow" | "down", detail) from one probe's outcome."""
    if not ok:
        return "down", detail or "no answer"
    if seconds is not None and seconds > slow_after:
        return "slow", detail or f"answered in {seconds:.1f} s"
    return "up", detail or (f"answered in {seconds:.1f} s" if seconds is not None else "answered")


def probe(url, kind="json", timeout=TIMEOUT, opener=None, clock=time.monotonic,
          slow_after=SLOW_AFTER):
    """{"state", "seconds", "detail"} for one small request.

    kind "json": the answer must parse as JSON and must not be an ArcGIS error object (the
    services answer HTTP 200 with {"error": ...}); kind "head": any answer at all will do."""
    opener = opener or _default_opener
    t0 = clock()
    try:
        resp = opener(url, timeout)
        try:
            raw = resp.read(READ_LIMIT)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - closing a fake / dead response is not a failure
                pass
        seconds = clock() - t0
        detail = ""
        if kind == "json":
            data = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(data, dict) and data.get("error"):
                err = data["error"]
                message = err.get("message") if isinstance(err, dict) else str(err)
                state, detail = classify(False, seconds, f"the service said: {message}")
                return {"state": state, "seconds": seconds, "detail": detail}
        state, detail = classify(True, seconds, detail, slow_after)
        return {"state": state, "seconds": seconds, "detail": detail}
    except urllib.error.HTTPError as e:
        seconds = clock() - t0
        # 504 / 502 is the "it is struggling" answer; 403 / 404 means the address moved
        why = f"HTTP {e.code}" + (" (gateway timeout)" if e.code in (502, 503, 504) else "")
        state, detail = classify(False, seconds, why)
        return {"state": state, "seconds": seconds, "detail": detail}
    except (ValueError, TypeError) as e:
        seconds = clock() - t0
        state, detail = classify(False, seconds, f"the answer was not usable ({e})")
        return {"state": state, "seconds": seconds, "detail": detail}
    except Exception as e:  # noqa: BLE001 - timeout, DNS, TLS, proxy: all of them are "down"
        seconds = clock() - t0
        state, detail = classify(False, seconds, str(e) or e.__class__.__name__)
        return {"state": state, "seconds": seconds, "detail": detail}


def check(services=None, timeout=TIMEOUT, opener=None, clock=time.monotonic,
          slow_after=SLOW_AFTER, workers=8):
    """[row] for every service, in the order of SERVICES.

    A row is {"key", "label", "url", "what", "state", "seconds", "detail"}. The probes run in
    parallel, so the whole check takes about one timeout even when everything is down."""
    services = list(services or SERVICES)
    out = [None] * len(services)

    def one(i):
        key, label, url, kind, what = services[i]
        r = probe(url, kind, timeout, opener, clock, slow_after)
        out[i] = {"key": key, "label": label, "url": url, "what": what,
                  "state": r["state"], "seconds": r["seconds"], "detail": r["detail"]}

    if workers and len(services) > 1:
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(workers, len(services))) as pool:
                list(pool.map(one, range(len(services))))
        except Exception:  # noqa: BLE001 - no threads: ask them one after another
            for i in range(len(services)):
                if out[i] is None:
                    one(i)
    else:
        for i in range(len(services)):
            one(i)
    return [r for r in out if r]


def by_state(rows, state):
    return [r for r in rows or [] if r.get("state") == state]


def worst(rows):
    for state in ("down", "slow", "up"):
        if by_state(rows, state):
            return state
    return "up"


def blocking(rows):
    """The services whose state should change what a user does before starting a run."""
    return [r for r in rows or []
            if r.get("key") in DECIDES and r.get("state") in ("down", "slow")]


def warning_text(rows):
    """One line to show before a run ("" when nothing needs saying)."""
    down = by_state(rows, "down")
    slow = by_state(rows, "slow")
    if not down and not slow:
        return ""
    parts = []
    if down:
        parts.append("down: " + ", ".join(r["label"] for r in down))
    if slow:
        parts.append("slow: " + ", ".join(r["label"] for r in slow))
    text = "; ".join(parts) + "."
    if any(r["key"] in DECIDES for r in down + slow):
        text += (" A pack build would wait a long time for the 3DHP service: set \"3DHP "
                 "flowlines and waterbodies\" to the National Map file, or leave it on "
                 "\"auto\", which now gives up on the service after a few minutes.")
    if [r for r in down if r["key"] not in DECIDES]:
        text += (" Those layers would be left out of the pack; the rest of the run is "
                 "unaffected and the layers already in the pack are kept.")
    return text


def text_report(rows, title="Service check"):
    """The rows as pasteable text."""
    lines = [title, "=" * len(title), ""]
    for r in rows or []:
        secs = "" if r.get("seconds") is None else f"{r['seconds']:.1f} s"
        lines.append("{:<5} {:<46} {:>7}  {}".format(r.get("state", "?"), r.get("label", ""),
                                                     secs, r.get("detail", "")))
    lines.append("")
    warn = warning_text(rows)
    lines.append(warn or "Every service answered.")
    return "\n".join(lines) + "\n"
