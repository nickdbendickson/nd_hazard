"""Inventory diff: what changed between two dam inventories, by dam ID. Pure Python.

Records are tool-1 style dicts (metric): {"dam_id", "name", "height_m", "inv_norm_m3",
"inv_max_m3", "status", "haz_existing", "x", "y"} with x / y in metres (EPSG:5070). A raw DWR
export is standardized first with inventory.standardize (the QGIS tool does that with tool 1's
field defaults), so a new DWR export can be compared with the last one or with a tool-1 layer.

Thresholds (engineer-chosen screening values with stated defaults; the tool shows them in
feet / acre-feet per house rule 3):
  * height   : changed when |new - old| >= HEIGHT_TOL_FT (0.5 ft)
  * storage  : normal or max storage changed when the relative change >= STORAGE_TOL_PCT (2 %)
               or the absolute change >= STORAGE_TOL_ACFT (1 ac-ft), whichever is larger
  * location : moved when the points are more than MOVE_M apart (50 m = 164 ft, YOUR value)
  * status, existing class, name: text compared case-insensitively after stripping.
A value that is missing on one side and present on the other is a change ("filled" /
"cleared"). The dam.json note is one line a reader can act on.
"""
import datetime
import math

FT_M, AF_M3 = 0.3048, 1233.4818375
HEIGHT_TOL_FT = 0.5
STORAGE_TOL_PCT = 2.0
STORAGE_TOL_ACFT = 1.0
MOVE_M = 50.0

# field -> (kind, label, unit for the report)
FIELDS = (("height_m", "num", "height", "ft"),
          ("inv_norm_m3", "vol", "normal storage", "ac-ft"),
          ("inv_max_m3", "vol", "max storage", "ac-ft"),
          ("status", "text", "status", ""),
          ("haz_existing", "text", "existing class", ""),
          ("name", "text", "name", ""))
CHANGE_KINDS = ("new", "removed", "changed")
CSV_COLUMNS = ["dam_id", "change", "field", "old", "new", "old_display", "new_display", "difference", "note"]


def _num(v):
    try:
        if v is None or v == "":
            return None
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def _txt(v):
    return "" if v is None else str(v).strip()


def display(field, v):
    """A value in the units the report shows (ft, ac-ft); text as it is."""
    if v is None or v == "":
        return ""
    if field == "height_m":
        return f"{float(v) / FT_M:.1f} ft"
    if field in ("inv_norm_m3", "inv_max_m3"):
        return f"{float(v) / AF_M3:,.1f} ac-ft"
    return str(v)


def compare_record(old, new, height_tol_ft=HEIGHT_TOL_FT, storage_tol_pct=STORAGE_TOL_PCT,
                   storage_tol_acft=STORAGE_TOL_ACFT, move_m=MOVE_M):
    """[(field, old, new, note)] of the attribute and location changes of one dam."""
    out = []
    for f, kind, label, unit in FIELDS:
        a, b = old.get(f), new.get(f)
        if kind == "text":
            ta, tb = _txt(a), _txt(b)
            if ta.casefold() != tb.casefold():
                out.append((f, ta, tb, f"{label} changed" if ta and tb else
                            (f"{label} filled" if tb else f"{label} cleared")))
            continue
        na, nb = _num(a), _num(b)
        if na is None and nb is None:
            continue
        if na is None or nb is None:
            out.append((f, na, nb, f"{label} {'filled' if nb is not None else 'cleared'}"))
            continue
        if kind == "num":
            d_ft = (nb - na) / FT_M
            if abs(d_ft) >= height_tol_ft:
                out.append((f, na, nb, f"{label} {d_ft:+.1f} ft"))
        else:
            d_af = (nb - na) / AF_M3
            rel = abs(nb - na) / max(abs(na), 1e-9) * 100.0
            if abs(d_af) >= storage_tol_acft and rel >= storage_tol_pct:
                out.append((f, na, nb, f"{label} {d_af:+,.1f} ac-ft ({rel:+.1f} %)".replace("(+", "(")))
    xa, ya, xb, yb = _num(old.get("x")), _num(old.get("y")), _num(new.get("x")), _num(new.get("y"))
    if None not in (xa, ya, xb, yb):
        d = math.hypot(xb - xa, yb - ya)
        if d > move_m:
            out.append(("location", f"{xa:.1f}, {ya:.1f}", f"{xb:.1f}, {yb:.1f}",
                        f"moved {d:,.0f} m ({d / FT_M:,.0f} ft)"))
    return out


def diff_inventories(old, new, **tol):
    """old, new: {dam_id: record}. Returns rows (list of dicts, CSV_COLUMNS) in dam-ID order:
    one row per new / removed dam, one row per changed field of a changed dam."""
    rows = []
    for did in sorted(set(old) | set(new), key=_sort_key):
        if did not in old:
            r = new[did]
            rows.append(_row(did, "new", "", None, None, note=f"not in the old inventory: {_txt(r.get('name'))} "
                             f"{display('height_m', r.get('height_m'))} {display('inv_norm_m3', r.get('inv_norm_m3'))}"
                             .strip()))
            continue
        if did not in new:
            r = old[did]
            rows.append(_row(did, "removed", "", None, None, note=f"not in the new inventory: {_txt(r.get('name'))} "
                             f"(old status {_txt(r.get('status')) or '?'})".strip()))
            continue
        for f, a, b, note in compare_record(old[did], new[did], **tol):
            rows.append(_row(did, "changed", f, a, b, note))
    return rows


def _sort_key(did):
    s = str(did)
    head = s.rstrip("0123456789")
    tail = s[len(head):]
    return (head, int(tail) if tail else -1, s)


def _row(did, change, field, old, new, note=""):
    diff = ""
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        if field == "height_m":
            diff = f"{(new - old) / FT_M:+.2f} ft"
        elif field in ("inv_norm_m3", "inv_max_m3"):
            diff = f"{(new - old) / AF_M3:+.1f} ac-ft"
    return {"dam_id": did, "change": change, "field": field, "old": "" if old is None else old,
            "new": "" if new is None else new, "old_display": display(field, old) if field != "location" else (old or ""),
            "new_display": display(field, new) if field != "location" else (new or ""),
            "difference": diff, "note": note}


def changed_ids(rows):
    """{dam_id: change kind} for every dam with a row ('changed' when any field changed)."""
    out = {}
    for r in rows:
        out.setdefault(r["dam_id"], r["change"])
    return out


def summary(rows):
    ids = changed_ids(rows)
    n = {k: sum(1 for v in ids.values() if v == k) for k in CHANGE_KINDS}
    by_field = {}
    for r in rows:
        if r["change"] == "changed":
            by_field[r["field"]] = by_field.get(r["field"], 0) + 1
    return {"dams": len(ids), "new": n["new"], "removed": n["removed"], "changed": n["changed"],
            "by_field": by_field}


def record_change(rows_of_dam, when=None):
    """The dam.json 'inventory_change' value for one dam: {"when", "fields": {field: {"old",
    "new"}}, "note"}. New / removed dams get "fields": {"inventory": {...}}."""
    when = when or datetime.datetime.now().isoformat(timespec="seconds")
    fields, notes = {}, []
    for r in rows_of_dam:
        if r["change"] in ("new", "removed"):
            fields["inventory"] = {"old": "absent" if r["change"] == "new" else "present",
                                   "new": "present" if r["change"] == "new" else "absent"}
            notes.append(f"{r['change']} dam: {r['note']}")
        else:
            fields[r["field"]] = {"old": r["old"], "new": r["new"]}
            notes.append(r["note"])
    return {"when": when, "fields": fields,
            "note": "Inventory changed (" + "; ".join(notes) + "): the screening of this dam should be "
                    "refreshed."}
