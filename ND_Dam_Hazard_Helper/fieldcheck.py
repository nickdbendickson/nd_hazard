# -*- coding: utf-8 -*-
"""2.30.0: the field-check sheet for the site visit after a screening High.

A screening High is argued over one question: are those buildings really habitable? The answer
comes from a site visit, and the visit needs a sheet - one page per structure with its address,
what the data says it is, the depth and velocity the model gives it, a place for a photograph
and a yes / no box for "habitable". This module writes that sheet and reads the marked-up copy
back into the run.

    rows = sheet_rows(structs, address_fn=address_of)
    write_sheet(folder, dam_name, rows)      # Field_Check_Sheet.md (one page per structure)
    write_csv(folder, rows)                  # Field_Check_Sheet.csv (the column to fill in)
    marked = read_csv(path)                  # the sheet after the visit
    applied = apply_to_store(store, structs, crs, ctx, marked)   # into the review decisions

Decisions:
  * The import writes the engineer's EXISTING habitability overrides - review.py's
    Structure_Review_Decisions.csv, matched by location, applied on every later run - and not
    a second mechanism of its own. A field visit is a review decision made outdoors; a parallel
    store would be a second truth about the same building.
  * "Habitable: no" is the only answer that can LOWER a class, so the import marks every such
    row in what it reports back ("lowers the class") and the caller shows them before the
    engineer saves. Nothing is applied until the decisions are saved and the classification is
    re-run, exactly as a decision made in the Review tab.
  * The sheet is Markdown and CSV, not a PDF form: both print, both open on a phone, and the
    CSV is what comes back. The photo slot is a file name the visitor writes down (or a printed
    box to tape a photo into) - the plugin does not manage photographs.
  * Which structures are listed: by default the ones that decide or could decide the class -
    the high / judgment zones, the unverified ones in them and the borderline ones - because
    that is what a visit after a screening High is for. `only_flagged=False` lists every
    flooded habitable structure.
  * The core (rows, text, CSV read and write) is pure Python; only apply_to_store needs QGIS,
    through review.ReviewStore.
"""

import csv
import io
import os
import time

SHEET_MD = "Field_Check_Sheet.md"
SHEET_CSV = "Field_Check_Sheet.csv"
# the columns the visitor fills in; everything before them is printed for reference
FILL_FIELDS = ["Habitable_Confirmed", "Type_Confirmed", "Persons_Observed", "Photo_File",
               "Visited_By", "Visited_Date", "Field_Note"]
FIELDS = ["Struct_ID", "Address", "City", "NSI_Type", "ACER_Type", "ACER_Zone", "ND_Outcome",
          "Depth_ft", "Velocity_fts", "Persons", "Why_Listed", "Lon", "Lat"] + FILL_FIELDS
# answers accepted in Habitable_Confirmed (case and punctuation are ignored). Anything
# else the visitor wrote ("could not tell", "nobody home") reads as unverified, which
# cannot lower a class - a sheet is never read as a clearance by accident.
YES = ("yes", "y", "true", "1", "habitable", "occupied")
NO = ("no", "n", "false", "0", "not habitable", "vacant", "shed", "outbuilding")
UNVERIFIED_BOX = "could not tell"          # the sheet's third box
# categories / curves, mirrored from structures.py so the sheet can be read without QGIS
CAT_DWELLING = "Dwelling"
CAT_OCCUPIED = "Occupied non-residential"
CAT_UNVERIFIED = "Unverified"
CAT_NOT_HABITABLE = "Not habitable"
TYPE_HOUSE = "House on foundation"
TYPE_MOBILE = "Mobile home"
_TYPE_WORDS = ((("mobile", "trailer", "manufactured"), TYPE_MOBILE),
               (("house", "foundation", "slab", "basement"), TYPE_HOUSE))
_CAT_WORDS = ((("dwelling", "home", "house", "residence", "residential"), CAT_DWELLING),
              (("business", "shop", "church", "occupied", "commercial", "non-residential"),
               CAT_OCCUPIED))


def _norm(text):
    return " ".join(str(text or "").strip().lower().replace("_", " ").split())


def _f(v, nd=2):
    try:
        if v is None or v == "":
            return None
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def why_listed(rec):
    """Why this structure is on the sheet (what the visit has to settle)."""
    out = []
    zone, outcome = rec.get("zone"), str(rec.get("outcome") or "")
    if rec.get("habitable") == "Unverified":
        out.append("habitability unknown in the data")
    if zone == 1:
        out.append("ACER high danger zone")
    elif zone == 2:
        out.append("ACER judgment zone")
    if rec.get("near"):
        out.append("borderline ({} boundary)".format(str(rec["near"]).lower()))
    if rec.get("acer_type") == TYPE_MOBILE:
        out.append("mobile home by the data - confirm")
    if rec.get("cut_off"):
        out.append("loses its only road access")
    if outcome and not out:
        out.append(outcome.lower())
    return "; ".join(out)


def is_flagged(rec):
    """The structures a visit after a screening High is for. A structure whose only reason is
    that it loses its road access is not one of them: the access screening is a network count,
    not a building's depth, and on a town's flood it lists hundreds of dry houses (Absaraka on
    2.32.0: 844 of 933 pages). `only_flagged=False` still lists them."""
    if rec.get("habitable") == "No":
        return False
    return bool(rec.get("zone") in (1, 2) or rec.get("habitable") == "Unverified"
                or rec.get("near"))


def sheet_rows(structs, address_fn=None, only_flagged=True, lonlat_fn=None):
    """[row dict] for the sheet, worst zone first. `address_fn(rec) -> (address, city, zip)`
    and `lonlat_fn(rec) -> (lon, lat)` are passed in so this module needs no QGIS."""
    rows = []
    for rec in structs or ():
        wet = rec.get("max_depth") is not None and (rec.get("max_depth") or 0) > 0
        if not (wet or rec.get("cut_off")):
            continue
        if only_flagged and not is_flagged(rec):
            continue
        if rec.get("habitable") == "No":
            continue
        addr = city = ""
        if address_fn is not None:
            try:
                addr, city = (address_fn(rec) + ("", "", ""))[:2]
            except Exception:                                    # noqa - no address
                addr = city = ""
        lon = lat = None
        if lonlat_fn is not None:
            try:
                lon, lat = lonlat_fn(rec)
            except Exception:                                    # noqa - no location
                lon = lat = None
        rows.append({
            "id": rec.get("id"), "address": addr or "", "city": city or "",
            "nsi_type": str(rec.get("occupancy") or rec.get("fema_category") or "")[:120],
            "acer_type": str(rec.get("acer_type") or ""), "zone": rec.get("zone"),
            "outcome": str(rec.get("outcome") or ""), "depth": _f(rec.get("max_depth")),
            "velocity": _f(rec.get("max_vel")), "persons": rec.get("persons"),
            "habitable": str(rec.get("habitable") or ""), "why": why_listed(rec),
            "lon": None if lon is None else round(float(lon), 6),
            "lat": None if lat is None else round(float(lat), 6)})
    rows.sort(key=lambda r: ({1: 0, 2: 1}.get(r["zone"], 2), str(r["address"]), r["id"] or 0))
    return rows


ZONE_LABELS = {1: "HIGH danger", 2: "JUDGMENT", 3: "Low (wet)"}


def page_text(row, dam_name="", index=None, total=None):
    """One structure's page, as the printed sheet."""
    def line(label, value):
        return "{:<22}{}".format(label + ":", "-" if value in (None, "") else value)
    head = "FIELD CHECK - {}".format(dam_name or "dam")
    if index is not None and total:
        head += "   (structure {} of {})".format(index, total)
    return "\n".join([
        head,
        "Structure ID {}   {}".format(row.get("id"), row.get("address") or "no address in the "
                                      "data"),
        "",
        line("Address", ", ".join(x for x in (row.get("address"), row.get("city")) if x)),
        line("What the data says", row.get("nsi_type")),
        line("ACER curve used", row.get("acer_type")),
        line("ACER zone", ZONE_LABELS.get(row.get("zone"), "-")),
        line("ND outcome", row.get("outcome")),
        line("Max depth (ft)", row.get("depth")),
        line("Max velocity (ft/s)", row.get("velocity")),
        line("People assumed", row.get("persons")),
        line("Habitable (data)", row.get("habitable")),
        line("On the sheet because", row.get("why")),
        line("Location (lon, lat)", "{}, {}".format(row.get("lon"), row.get("lat"))
             if row.get("lon") is not None else ""),
        "",
        "CONFIRM ON SITE",
        "  Habitable?            [ ] {}    [ ] {}    [ ] {}".format(YES[0], NO[0],
                                                            UNVERIFIED_BOX),
        "  If habitable, it is   [ ] a dwelling    [ ] occupied non-residential",
        "  Built as              [ ] house on a foundation    [ ] mobile / manufactured home",
        "  People (if known)     ______",
        "  Photograph            file name: ______________________   (or tape a print here)",
        "",
        "  +----------------------------------------------------------------+",
        "  |                                                                |",
        "  |                        photo                                   |",
        "  |                                                                |",
        "  +----------------------------------------------------------------+",
        "",
        "  Visited by ____________________   Date __________",
        "  Notes ______________________________________________________________",
        "  ____________________________________________________________________",
        "",
        "Write the same answers into the row of {} with this Struct_ID, then import the file "
        "in the Review tab ('Import field-check sheet...') - the answers become engineer "
        "review decisions and apply on the next classification run.".format(SHEET_CSV),
    ])


def sheet_text(rows, dam_name="", when=None):
    """The whole sheet: a cover note, then one page per structure."""
    when = when or time.strftime("%Y-%m-%d")
    out = ["FIELD-CHECK SHEET - {} - {}".format(dam_name or "dam", when),
           "",
           "{} structure(s) to confirm on site. One page each. The pages list what the data "
           "says; the boxes are what the visit settles. 'Habitable: no' is the only answer "
           "that can lower the hazard class, so it is shown to the engineer before it is "
           "saved.".format(len(rows)),
           ""]
    for i, row in enumerate(rows, start=1):
        out.append("\f" if i > 1 else "")
        out.append(page_text(row, dam_name, i, len(rows)))
        out.append("")
    return "\n".join(out)


def write_sheet(folder, dam_name, rows, name=SHEET_MD, when=None):
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(sheet_text(rows, dam_name, when))
    return path


def csv_row(row):
    return [row.get("id"), row.get("address"), row.get("city"), row.get("nsi_type"),
            row.get("acer_type"), ZONE_LABELS.get(row.get("zone"), ""), row.get("outcome"),
            row.get("depth"), row.get("velocity"), row.get("persons"), row.get("why"),
            row.get("lon"), row.get("lat")] + [""] * len(FILL_FIELDS)


def write_csv(folder, rows, name=SHEET_CSV):
    """The sheet as a CSV with the columns the visitor fills in left empty."""
    path = os.path.join(folder, name)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for row in rows or ():
            w.writerow(["" if v is None else v for v in csv_row(row)])
    return path


def _answer(text):
    """"yes" / "no" / "unverified" / "" from a cell the visitor wrote."""
    t = _norm(text).strip(" .")
    if not t:
        return ""
    # the first word only: "not sure" must never read as the "no" that lowers a class
    first = t.split()[0].strip("[]()")
    if t in YES or first in YES:
        return "yes"
    if t in NO or first in NO:
        return "no"
    # anything else the visitor wrote ("could not tell", "nobody home") is not an answer:
    # it is kept as unverified, which cannot lower the class, so the row is not lost
    return "unverified"


def _type_answer(text):
    t = _norm(text)
    for words, value in _TYPE_WORDS:
        if any(w in t for w in words):
            return value
    return ""


def _category(answer, type_text, data_category=""):
    """The review-decision category for one row ("" when the row decides nothing)."""
    if answer == "no":
        return CAT_NOT_HABITABLE
    if answer == "unverified":
        return CAT_UNVERIFIED
    if answer != "yes":
        return ""
    t = _norm(type_text)
    for words, value in _CAT_WORDS:
        if any(w in t for w in words):
            return value
    return CAT_DWELLING if _norm(data_category) in ("", "dwelling") else CAT_OCCUPIED


def _num(v):
    t = str(v or "").strip().replace(",", "")
    try:
        return int(float(t)) if t else None
    except ValueError:
        return None


def parse_rows(text):
    """The marked-up sheet's rows: [dict] with the answers normalised. Tolerant of Excel
    copies (';' as the list separator, headers in another case, extra columns)."""
    data = str(text or "")
    if not data.strip():
        return []
    sample = data[:4096]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(data), delimiter=delim)
    canon = {}
    for name in reader.fieldnames or ():
        canon[_norm(name).replace(" ", "_")] = name
    out = []
    for raw in reader:
        def g(field):
            key = canon.get(_norm(field).replace(" ", "_"))
            return "" if key is None else (raw.get(key) or "")
        answer = _answer(g("Habitable_Confirmed"))
        typ = _type_answer(g("Type_Confirmed"))
        note = str(g("Field_Note") or "").strip()
        row = {"id": _num(g("Struct_ID")), "address": str(g("Address") or "").strip(),
               "lon": _f(g("Lon"), 7), "lat": _f(g("Lat"), 7),
               "answer": answer, "acer_type": typ,
               "category": _category(answer, g("Type_Confirmed"), g("NSI_Type")),
               "persons": _num(g("Persons_Observed")),
               "photo": str(g("Photo_File") or "").strip(),
               "by": str(g("Visited_By") or "").strip(),
               "date": str(g("Visited_Date") or "").strip(), "note": note}
        if not (row["answer"] or row["acer_type"] or row["persons"] is not None or note):
            continue                      # a row nobody filled in
        out.append(row)
    return out


def read_csv(path):
    """parse_rows for a file, in either encoding Excel writes."""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, encoding=enc) as fh:
                return parse_rows(fh.read())
        except (UnicodeDecodeError, csv.Error):
            continue
    raise ValueError("{} could not be read as a CSV".format(os.path.basename(path)))


def decision_note(row):
    """The note the review decision carries, so the file says where it came from."""
    bits = ["field check"]
    if row.get("by"):
        bits.append("by {}".format(row["by"]))
    if row.get("date"):
        bits.append("on {}".format(row["date"]))
    if row.get("photo"):
        bits.append("photo {}".format(row["photo"]))
    if row.get("note"):
        bits.append(row["note"])
    return "; ".join(bits)


def lowers_class(row):
    """True when this answer could lower the hazard class (habitable: no)."""
    return row.get("category") == CAT_NOT_HABITABLE


def summary(rows):
    """Counts of a parsed sheet, for the message the window shows."""
    rows = list(rows or ())
    return {"rows": len(rows),
            "habitable_yes": sum(1 for r in rows if r.get("answer") == "yes"),
            "habitable_no": sum(1 for r in rows if r.get("answer") == "no"),
            "unverified": sum(1 for r in rows if r.get("answer") == "unverified"),
            "types": sum(1 for r in rows if r.get("acer_type")),
            "persons": sum(1 for r in rows if r.get("persons") is not None),
            "lowering": sum(1 for r in rows if lowers_class(r))}


def match_rows(rows, structs):
    """[(row, structure record or None)] - matched by Struct_ID, then by address."""
    by_id = {rec.get("id"): rec for rec in structs or ()}
    by_addr = {}
    for rec in structs or ():
        for src in rec.get("sources", ()) or ():
            t = _norm(src.get("addr_text"))
            if t:
                by_addr.setdefault(t, rec)
    out = []
    for row in rows or ():
        rec = by_id.get(row.get("id"))
        if rec is None and row.get("address"):
            rec = by_addr.get(_norm(row["address"]))
        out.append((row, rec))
    return out


def apply_to_store(store, structs, crs, ctx, rows, reviewer=None):
    """Write the sheet's answers into the engineer's review decisions (review.ReviewStore -
    the same file the Review tab saves). Returns {"applied", "unmatched", "lowering",
    "rows": [(struct id, what)]}; the caller saves the store and re-runs the classification."""
    done, unmatched, lowering, what = 0, [], 0, []
    for row, rec in match_rows(rows, structs):
        if rec is None:
            unmatched.append(row.get("id") if row.get("id") is not None
                             else row.get("address") or "?")
            continue
        category = row.get("category") or None
        acer = row.get("acer_type") or None
        persons = row.get("persons")
        if not (category or acer or persons is not None or row.get("note")):
            continue
        try:
            store.set_decision(rec, crs, ctx, category=category, acer_type=acer,
                               persons=persons, note=decision_note(row), reviewer=reviewer)
        except Exception as e:                                   # noqa - one bad row only
            unmatched.append("{} ({})".format(row.get("id"), e))
            continue
        done += 1
        if lowers_class(row):
            lowering += 1
        what.append((rec.get("id"), "habitable: {}{}{}".format(
            row.get("answer") or "-", ", {}".format(acer) if acer else "",
            ", {} people".format(persons) if persons is not None else "")))
    return {"applied": done, "unmatched": unmatched, "lowering": lowering, "rows": what}
