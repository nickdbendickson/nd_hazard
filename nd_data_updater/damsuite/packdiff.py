"""What changed in the offline pack, and whose results that makes stale (1.16.0).

The updater refreshes layers; the question nobody could answer before is "so which of the dams
I have already classified would come out differently now?". This module answers it from the two
pack manifests alone - no layer is opened - and says which dam folders to re-run.

    changes = diff(old_manifest, new_manifest)       # [change, ...]
    report  = report_text(changes)
    stale   = stale_dams(scan_rows, changes)         # [(row, why), ...]

A change is a dict: {"name", "group", "kind", "was", "now", "delta", "affects", "text"} where
kind is "added" | "removed" | "refreshed" | "grew" | "shrank" | "unchanged" and `affects` is
True when that layer can change a hazard result.

Which layers affect a result (AFFECTING): the ones a classification counts or routes over -
structures and addresses (habitable buildings and population), roads and railroads (access and
the ND railroad rule), the lifelines, and the dam inventories (a dam that did not exist in the
old pack). Terrain, land cover and hydrography change *how* a flood is modelled rather than who
is counted, so they are listed as "model inputs": a change there means re-run the model, not
only the classification, which is a bigger statement and is reported separately.

`stale_dams` is deliberately conservative where it cannot be sure: without a flood footprint for
a dam it says the dam is possibly affected rather than silently clearing it. A caller with QGIS
can narrow the list by testing the new features against each dam's flood polygon and pass the
result in as `footprints`.

Nothing here needs QGIS. Pure Python standard library.
"""
import os

# layer name prefixes -> what a change to them means
CONSEQUENCE = ("NSI", "USA_STRUCTURES", "MS_BUILDINGS", "ADDRESS", "ND_ADDRESS", "PARCEL",
               "FEMA_PARCEL", "OSM_PLACES", "OSM_TRAILER", "E911")
ACCESS = ("ROADS", "ND_ROADS", "COUNTY_ROADS", "HPMS", "NBI", "RAIL", "NARN", "RAILROAD",
          "CROSSINGS", "SARP_CROSSINGS", "AADT", "NDDOT")
LIFELINE = ("HIFLD", "LIFELINE", "OSM_PARKS")
DAMS = ("NID", "DWR_DAMS", "DWR_DAM_LINES", "SARP_DAMS", "DWR_UNKNOWN", "DWR_DUGOUT")
MODEL = ("NLCD", "LANDCOVER", "DEM", "TERRAIN", "NHD", "3DHP", "FLOWLINE", "WATERBODY")
ROLES = (("consequence", CONSEQUENCE, "who is counted in the flood"),
         ("access", ACCESS, "roads, railroads and access"),
         ("lifeline", LIFELINE, "the lifelines"),
         ("dams", DAMS, "the dam inventory"),
         ("model", MODEL, "how the flood is modelled"))
KINDS = ("added", "removed", "grew", "shrank", "refreshed", "unchanged")
# a feature count that moves by less than this fraction is "refreshed", not "grew" / "shrank":
# the services re-issue slightly different counts for the same data
NOISE = 0.001


def role(name):
    """("consequence" | "access" | "lifeline" | "dams" | "model" | "other", why)."""
    up = str(name or "").upper()
    for key, prefixes, why in ROLES:
        for p in prefixes:
            if up.startswith(p) or p in up:
                return key, why
    return "other", "not used by the classification"


def affects_result(name):
    """Does a change to this layer change who is counted or how they are reached?"""
    return role(name)[0] in ("consequence", "access", "lifeline", "dams")


def _sets(manifest, kind="datasets"):
    """{name: entry} from a pack manifest (either format, either nesting)."""
    if not isinstance(manifest, dict):
        return {}
    d = manifest.get(kind)
    if isinstance(d, dict):
        return {k: (v if isinstance(v, dict) else {}) for k, v in d.items()}
    return {}


def _count(entry):
    for key in ("count", "features", "n"):
        try:
            v = entry.get(key)
        except AttributeError:
            return None
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def _when(entry):
    try:
        return str(entry.get("fetched") or entry.get("when") or "")
    except AttributeError:
        return ""


def diff(old, new, kind="datasets", include_unchanged=False):
    """[change, ...] between two pack manifests, biggest first."""
    a, b = _sets(old, kind), _sets(new, kind)
    out = []
    for name in sorted(set(a) | set(b)):
        ea, eb = a.get(name), b.get(name)
        ca = _count(ea) if ea is not None else None
        cb = _count(eb) if eb is not None else None
        key, why = role(name)
        group = (eb or ea or {}).get("group") or ""
        if ea is None:
            k, delta = "added", cb
        elif eb is None:
            k, delta = "removed", -(ca or 0)
        elif ca is None or cb is None:
            k, delta = ("refreshed" if _when(ea) != _when(eb) else "unchanged"), None
        else:
            delta = cb - ca
            base = max(1, ca)
            if delta > 0 and abs(delta) / base > NOISE:
                k = "grew"
            elif delta < 0 and abs(delta) / base > NOISE:
                k = "shrank"
            else:
                k = "refreshed" if _when(ea) != _when(eb) else "unchanged"
        if k == "unchanged" and not include_unchanged:
            continue
        out.append({"name": name, "group": group, "kind": k, "was": ca, "now": cb,
                    "delta": delta, "role": key, "why": why,
                    "affects": key in ("consequence", "access", "lifeline", "dams"),
                    "text": _change_text(name, k, ca, cb, delta)})
    out.sort(key=lambda c: (-abs(c["delta"] or 0), KINDS.index(c["kind"]) if c["kind"] in KINDS
                            else 9, c["name"]))
    return out


def _change_text(name, kind, was, now, delta):
    if kind == "added":
        return "{}: new in the pack ({} features)".format(name, now if now is not None else "?")
    if kind == "removed":
        return "{}: no longer in the pack (was {})".format(name, was if was is not None else "?")
    if kind in ("grew", "shrank"):
        return "{}: {} -> {} features ({}{})".format(name, was, now,
                                                     "+" if (delta or 0) > 0 else "", delta)
    if kind == "refreshed":
        return "{}: fetched again, same count".format(name)
    return "{}: unchanged".format(name)


def by_role(changes):
    out = {}
    for c in changes or []:
        out.setdefault(c["role"], []).append(c)
    return out


def counts(changes):
    out = {k: 0 for k in KINDS}
    out["affecting"] = 0
    out["model"] = 0
    for c in changes or []:
        if c["kind"] in out:
            out[c["kind"]] += 1
        if c["affects"]:
            out["affecting"] += 1
        if c["role"] == "model":
            out["model"] += 1
    out["layers"] = len(changes or [])
    return out


def summary_text(changes):
    c = counts(changes)
    if not c["layers"]:
        return "Nothing changed in the pack."
    parts = []
    for k in ("added", "removed", "grew", "shrank", "refreshed"):
        if c[k]:
            parts.append("{} {}".format(c[k], k))
    text = "{} layer(s) changed: {}.".format(c["layers"], ", ".join(parts))
    if c["affecting"]:
        text += " {} of them change who is counted or how they are reached, so results " \
                "from before this refresh may be out of date.".format(c["affecting"])
    if c["model"]:
        text += " {} model input(s) changed: those dams need the breach model run again, not " \
                "only the classification.".format(c["model"])
    if not c["affecting"] and not c["model"]:
        text += " None of them changes a hazard result."
    return text


def report_text(changes, title="Pack change report"):
    lines = [title, "=" * len(title), "", summary_text(changes), ""]
    groups = by_role(changes)
    for key, _prefixes, why in ROLES:
        rows = groups.get(key) or []
        if not rows:
            continue
        lines.append("{} ({}):".format(key.capitalize(), why))
        for c in rows:
            lines.append("  " + c["text"])
        lines.append("")
    other = groups.get("other") or []
    if other:
        lines.append("Other layers:")
        lines += ["  " + c["text"] for c in other]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- stale dams --
def stale_dams(rows, changes, footprints=None):
    """[(row, why)] for the dams whose result was produced before this refresh and that one of
    the changed layers can move.

    `rows` are damsuite.suite_home.scan rows. A dam counts when it has a computed class (there
    is a result to invalidate). `footprints` is an optional {dam_id: bool} from a caller that
    tested the new features against each dam's flood polygon: False clears that dam, a missing
    entry leaves it in the list (this module never claims a dam is unaffected on no evidence)."""
    affecting = [c for c in changes or [] if c["affects"]]
    model = [c for c in changes or [] if c["role"] == "model"]
    if not affecting and not model:
        return []
    names = ", ".join(c["name"] for c in (affecting + model)[:4])
    if len(affecting) + len(model) > 4:
        names += ", ..."
    out = []
    for r in rows or []:
        if not r.get("class"):
            continue
        if footprints is not None and footprints.get(r.get("dam_id")) is False:
            continue
        why = []
        if affecting:
            why.append("re-classify")
        if model:
            why.append("re-run the model")
        sure = "" if footprints is None else " (new features fall in its flood)" \
            if footprints.get(r.get("dam_id")) else " (not checked against its flood)"
        out.append((r, "{}: {} changed{}".format(" and ".join(why), names, sure)))
    return out


def stale_text(stale, changes=None):
    if not stale:
        return "No classified dam is affected by this refresh."
    ids = [r.get("dam_id") or os.path.basename(r.get("folder") or "") for r, _w in stale]
    text = "{} classified dam(s) may be out of date: {}".format(
        len(stale), ", ".join(ids[:12]) + (", ..." if len(ids) > 12 else ""))
    if changes:
        text += ". " + summary_text(changes)
    return text
