"""Which dams to look at first (1.16.0): computed class against the class on record.

After a county or statewide screen the question is not "what is each dam" but "which of these
does somebody have to deal with". This module ranks the dam folders of a Dams folder, worst
disagreement first, so the dam safety programme gets a review list instead of a pile of folders.

    rows = rank(scan_rows)          # damsuite.suite_home.scan(dams_folder) rows
    text = summary_text(rows)
    csv  = to_csv(rows)             # or xlsx_lite for a spreadsheet

Ranking, in order:
  1. "raise": the suite computed a HIGHER class than the class on record. These matter most -
     a dam is being managed as less dangerous than it screens.
  2. "incomplete": a class was computed but the run is flagged (flood cut off, truncated at the
     terrain edge, model not run), so the class is a floor, not an answer.
  3. "lower": computed LOWER than the record. Worth checking, nobody is in danger meanwhile.
  4. "agree": same tier. Nothing to do.
  5. "unknown": no class computed yet (not screened, or no record).
Within a group: higher computed class first, then by dam ID.

Two vocabularies are compared. The ND rule gives High / Medium / Low; ACER TM 11 and the DWR
inventory give High / Significant / Low. "Medium" and "Significant" are the same middle tier, so
a computed Medium against a DWR Significant is an agreement, not a disagreement - getting this
wrong would fill the list with 200 false mismatches.

Nothing here reads a flood raster or re-runs anything: it only reads what the runs already
wrote (dam.json, through suite_home.scan). Pure Python standard library.
"""
import os

# tier -> rank; both vocabularies, lower-cased, plus the spellings seen in DWR exports
TIERS = {"low": 1, "l": 1, "minimal": 1,
         "medium": 2, "med": 2, "m": 2, "significant": 2, "sig": 2, "s": 2, "moderate": 2,
         "high": 3, "h": 3}
LABELS = {1: "Low", 2: "Medium / Significant", 3: "High"}
GROUPS = ("raise", "incomplete", "lower", "agree", "unknown")
GROUP_TEXT = {
    "raise": "computed higher than the class on record",
    "incomplete": "computed, but the run is incomplete",
    "lower": "computed lower than the class on record",
    "agree": "computed class agrees with the record",
    "unknown": "no class computed yet",
}
GROUP_ORDER = {g: i for i, g in enumerate(GROUPS)}
# words in a dam.json note / problem that mean the class is a floor rather than an answer
INCOMPLETE_WORDS = ("cut off", "cut_off", "truncat", "terrain edge", "domain-run limit",
                    "domain run limit", "duration cap", "incomplete", "not converged")
COLUMNS = ("Dam ID", "Name", "Computed", "On record", "Verdict", "Why", "Flags", "Next",
           "Folder")


def tier(text):
    """Class text -> 1 / 2 / 3, or None when it is empty or not a class."""
    t = str(text or "").strip().lower()
    if not t:
        return None
    t = t.split("(")[0].strip()
    if t in TIERS:
        return TIERS[t]
    for word, rank in TIERS.items():
        if len(word) > 2 and word in t:
            return rank
    return None


def _flags(row):
    """The run flags that make a computed class a floor ("" when the run looks complete)."""
    found = []
    for key in ("problem", "next_text", "note", "incomplete", "flags"):
        text = str(row.get(key) or "").lower()
        for word in INCOMPLETE_WORDS:
            if word in text and word not in found:
                found.append(word)
    if row.get("class") and not row.get("has_rasters") and not row.get("has_manifest"):
        found.append("no model run")
    return "; ".join(found)


def verdict(row):
    """(group, why) for one scan row."""
    comp, rec = tier(row.get("class")), tier(row.get("existing_class"))
    flags = _flags(row)
    if comp is None:
        return "unknown", ("not screened or classified yet" if not row.get("problem")
                           else str(row.get("problem")))
    if rec is None:
        # nothing to compare against; an incomplete run still deserves a second look
        return ("incomplete" if flags else "unknown"), (
            "computed {}, no class on record".format(LABELS.get(comp, comp)))
    if comp > rec:
        return "raise", "computed {}, on record {}".format(LABELS.get(comp), LABELS.get(rec))
    if flags:
        return "incomplete", "computed {} but the run is incomplete".format(LABELS.get(comp))
    if comp < rec:
        return "lower", "computed {}, on record {}".format(LABELS.get(comp), LABELS.get(rec))
    return "agree", "both {}".format(LABELS.get(comp))


def rank(rows):
    """Scan rows -> the same rows with "verdict", "why", "flags", "computed_tier",
    "record_tier" and "order", worst first. The input is not changed."""
    out = []
    for r in rows or []:
        row = dict(r)
        group, why = verdict(row)
        row["verdict"] = group
        row["why"] = why
        row["flags"] = _flags(row)
        row["computed_tier"] = tier(row.get("class"))
        row["record_tier"] = tier(row.get("existing_class"))
        out.append(row)
    out.sort(key=lambda r: (GROUP_ORDER.get(r["verdict"], 9),
                            -(r["computed_tier"] or 0),
                            str(r.get("dam_id") or "")))
    for i, r in enumerate(out):
        r["order"] = i + 1
    return out


def counts(rows):
    out = {g: 0 for g in GROUPS}
    for r in rows or []:
        g = r.get("verdict")
        if g in out:
            out[g] += 1
    out["dams"] = len(rows or [])
    return out


def summary_text(rows):
    c = counts(rows)
    if not c["dams"]:
        return "No dams to rank."
    parts = ["{} dam(s)".format(c["dams"])]
    for g in GROUPS:
        if c[g]:
            parts.append("{} {}".format(c[g], g))
    text = ", ".join(parts)
    if c["raise"]:
        text += ". Start at the top: {} dam(s) screen higher than the class on record.".format(
            c["raise"])
    elif c["incomplete"]:
        text += ". {} run(s) are incomplete, so their class is a floor.".format(c["incomplete"])
    else:
        text += ". Nothing screens higher than its record."
    return text


def attention(rows):
    """The rows a human has to look at (raise + incomplete) - the review list."""
    return [r for r in rows or [] if r.get("verdict") in ("raise", "incomplete")]


def table(rows):
    """[[header], [row], ...] in COLUMNS order, for a spreadsheet or a QTableWidget."""
    out = [list(COLUMNS)]
    for r in rows or []:
        out.append([r.get("dam_id", ""), r.get("name", ""), r.get("class", ""),
                    r.get("existing_class", ""), r.get("verdict", ""), r.get("why", ""),
                    r.get("flags", ""), r.get("next_text", ""), r.get("folder", "")])
    return out


def _csv_cell(v):
    s = "" if v is None else str(v)
    return '"' + s.replace('"', '""') + '"' if any(c in s for c in ',"\n\r') else s


def to_csv(rows):
    return "\r\n".join(",".join(_csv_cell(c) for c in line) for line in table(rows)) + "\r\n"


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(to_csv(rows))
    return path


def write_xlsx(path, rows, sheet="Priority"):
    """A spreadsheet through damsuite.xlsx_lite (no third-party library): every dam, then the
    review list on its own sheet."""
    from . import xlsx_lite as XL
    sheets = [(sheet, table(rows))]
    need = attention(rows)
    if need:
        sheets.append(("Review list", table(need)))
    XL.write(path, sheets)
    return path


def scan_and_rank(dams_folder):
    """The whole thing for a Dams folder: rows, worst first ([] when it cannot be read)."""
    if not dams_folder or not os.path.isdir(dams_folder):
        return []
    from . import suite_home as SH
    return rank(SH.scan(dams_folder))
