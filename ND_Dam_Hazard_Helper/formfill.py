# -*- coding: utf-8 -*-
"""v2.9: fill the engineer's own form template with the run's values.

The state's hazard-classification form is NOT part of the plugin and no form
field is guessed here. Instead:

  available_values(output_folder)  the values the plugin can vouch for, read
        from the files of the last run only (QA/Last_Run_Snapshot.json,
        Hazard_Classification_Summary.txt, the run log, the Classification /
        QA / review tables), as an ordered dict KEY -> text (None when the run
        did not record it). FORM_KEYS lists every KEY with where it comes from.
  write_values_csv(output_folder)  Form_Values.csv (KEY, Value, Description)
        for copying values into any form by hand (e.g. a fillable PDF).
  fill_template(template, values, out)  a template of the engineer's (the
        form's layout typed or pasted into .txt / .md / .html / .csv / .docx)
        with {{KEY}} placeholders where the values go. A .docx is edited as its
        XML (zipfile, no python-docx): Word splits text into runs, so the runs a
        placeholder spans are merged into the run where it starts (that run's
        formatting is kept); every other part, run and attribute is left as it
        was. Returns (out path, unknown placeholders, placeholders without a
        value). Unknown and value-less placeholders are left in the output as
        they are (visible), unless missing_text is given.

Values are the plugin's SCREENING results and data - the engineer checks
every value that goes on a signed form.
"""

import collections
import csv
import html
import os
import re
import time
import zipfile
from xml.sax.saxutils import escape as _xml_escape

from . import history as HIS
from . import portfolio as PF
from .constants import PLUGIN_NAME, OUTPUT_FOLDER_NAME, SUBDIR_QA, SUBDIR_CLASSIFICATION

VALUES_CSV = "Form_Values.csv"
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}")
TEXT_EXTS = (".txt", ".text", ".md", ".markdown")
HTML_EXTS = (".html", ".htm")
REVIEW_FILE = "Structure_Review_Decisions.csv"          # review.REVIEW_FILE

# KEY, description (where the value comes from). Order = the order of Form_Values.csv.
FORM_KEYS = [
    ("DAM_NAME", "Dam name (run snapshot, else the summary header)"),
    ("NID_ID", "National Inventory of Dams ID (dam_location.json in the dam folder, else the "
               "run's subject_dam_nid setting, else the summary's NID section)"),
    ("RUN_DATE", "Date and time the classification run finished (run log, else the snapshot)"),
    ("TODAY", "Date these values were read"),
    ("PLUGIN_NAME", "Name of the plugin"),
    ("PLUGIN_VERSION", "Plugin version that made the run (run log / summary header)"),
    ("HYDRAULIC_MODEL", "Hydraulic model type as the summary states it (1D / 2D)"),
    ("GOVERNING_SCENARIO", "Governing breach scenario (snapshot)"),
    ("ND_CLASS", "ND hazard potential classification, screening result (High / Medium / "
                 "Low)"),
    ("ND_BASIS", "Basis of the ND classification as the summary states it"),
    ("ND_GOVERNING_CRITERION", "First ND reason of the summary - the criterion that sets the "
                               "class"),
    ("ND_GOVERNING_SECTION", "ND standards section of that reason (the [..] at its end)"),
    ("ND_REASONS", "All ND reasons of the summary, '; ' separated"),
    ("TM11_CLASS", "USBR ACER TM 11 classification, screening result"),
    ("LIVES_IN_JEOPARDY", "TM 11 lives in jeopardy (snapshot, else the summary)"),
    ("TM11_WITHOUT_JUDGMENT", "TM 11 class without the judgment-zone items (summary)"),
    ("PAR", "Population at risk used by the loss-of-life screening (summary CONSEQUENCES)"),
    ("EST_LIFE_LOSS", "Estimated fatalities of the loss-of-life screening (summary)"),
    ("EST_LIFE_LOSS_RANGE", "Range of the estimated fatalities (summary)"),
    ("PAR_NIGHT", "Population at risk at night (summary CONSEQUENCES)"),
    ("PAR_DAY", "Population at risk in the day (summary CONSEQUENCES)"),
    ("EST_LIFE_LOSS_NIGHT", "Estimated fatalities at night (summary)"),
    ("EST_LIFE_LOSS_DAY", "Estimated fatalities in the day (summary)"),
    ("FLOODED_HABITABLE", "Habitable structures flooded in the governing scenario "
                          "(snapshot)"),
    ("FLOODED_HABITABLE_HIGH", "Habitable structures in the ACER high danger zone (summary "
                               "table)"),
    ("FLOODED_HABITABLE_JUDGMENT", "Habitable structures in the ACER judgment zone (summary)"),
    ("FLOODED_HABITABLE_LOW", "Habitable structures in the ACER low danger zone, wet "
                              "(summary)"),
    ("FLOODED_HOUSES_HIGH", "Houses on foundation in the high danger zone (summary)"),
    ("FLOODED_HOUSES_JUDGMENT", "Houses on foundation in the judgment zone (summary)"),
    ("FLOODED_HOUSES_LOW", "Houses on foundation in the low danger zone, wet (summary)"),
    ("FLOODED_MOBILE_HOMES_HIGH", "Mobile homes in the high danger zone (summary)"),
    ("FLOODED_MOBILE_HOMES_JUDGMENT", "Mobile homes in the judgment zone (summary)"),
    ("FLOODED_MOBILE_HOMES_LOW", "Mobile homes in the low danger zone, wet (summary)"),
    ("UNVERIFIED_FLOODED", "Unverified structures in the flooded area (summary)"),
    ("CUT_OFF_HABITABLE", "Dry habitable structures that lose road access (snapshot)"),
    ("ROADS_OVERTOPPED", "Overtopped road stretches (summary ROADS line)"),
    ("ROADS_OVERTOPPED_HIGH", "Overtopped road stretches of ND class High (summary)"),
    ("ROADS_OVERTOPPED_MEDIUM", "Overtopped road stretches of ND class Medium (summary)"),
    ("ROADS_MAX_AADT", "Highest AADT of the overtopped roads (Classification/"
                       "Roads_Inundated.csv)"),
    ("ROADS_MAX_AADT_ROAD", "Name of the road with that AADT"),
    ("RAIL_INUNDATED", "Inundated rail stretches (summary RAIL line)"),
    ("RAIL_LINES", "Railroad / subdivision of the inundated rail (Rail_Inundated.csv)"),
    ("LIFELINES_INUNDATED", "Lifeline / critical facilities inundated (snapshot)"),
    ("LIFELINES_LIST", "Their names (category), '; ' separated"),
    ("OTHER_DAMS_INUNDATED", "Other dams in the inundated area (Classification/"
                             "Dams_In_Series.csv, Flooded = Yes, the dam itself left out)"),
    ("OTHER_DAMS_LIST", "Their names (NID ID), '; ' separated"),
    ("FIRST_ARRIVAL_HR", "Earliest first arrival (hours after breach) at a flooded habitable "
                         "structure (Habitable_Structures.csv)"),
    ("FIRST_ARRIVAL_STRUCTURE", "ID of that structure (Struct_ID)"),
    ("REVIEWERS", "Names in the Reviewer column of Structure_Review_Decisions.csv"),
    ("REVIEW_DECISIONS", "Number of saved structure review decisions"),
    ("TERMINATION_POINT", "Downstream termination of the incremental hazard "
                          "(QA/Termination_Point.csv: status, river mile)"),
    ("TERMINATION_RIVER_MILE", "River mile of the termination point"),
    ("SENSITIVITY_VERDICT", "Verdict of the depth / velocity sensitivity table (summary)"),
    ("OLDEST_DATA_DATE", "Oldest date of QA/Data_Vintage.csv (the plugin row left out)"),
]
KEY_DESCRIPTIONS = collections.OrderedDict(FORM_KEYS)
_DRY = ("", None, "Not inundated")


def _num(text):
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _fmt(v, nd=None):
    if v is None:
        return None
    if isinstance(v, float) and nd is not None:
        return "{:.{}f}".format(v, nd)
    return str(v)


def _slug(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", str(name or "")).strip("_")[:40] or "Scenario"


def _table(out, base, governing):
    folder = os.path.join(out, SUBDIR_CLASSIFICATION)
    for n in (["{}_{}.csv".format(base, _slug(governing))] if governing else []) + \
            [base + ".csv"]:
        p = os.path.join(folder, n)
        if os.path.isfile(p):
            return p
    return None


def available_values(output_folder):
    """Ordered dict KEY -> text (None = not recorded by the run) for the KEYs
    of FORM_KEYS, read from the files in `output_folder` (the dam's outputs
    folder). Never raises for a missing or unreadable file."""
    out = output_folder
    qa = os.path.join(out, SUBDIR_QA)
    v = collections.OrderedDict((k, None) for k, _d in FORM_KEYS)
    snap = HIS.read_snapshot(qa) or {}
    parsed = PF.parse_summary(PF.read_text(PF.newest(out, "Hazard_Classification_Summary*.txt")
                                           or "") or "")
    sf = PF.summary_facts(parsed)
    log = PF.parse_run_log(PF.read_text(PF.newest(out, "Hazard_Classification_Run_Log*.txt")
                                        or "") or "")
    dam_folder = os.path.dirname(os.path.abspath(out)) \
        if os.path.basename(os.path.normpath(out)).lower() == OUTPUT_FOLDER_NAME else out
    v["DAM_NAME"] = snap.get("dam") or parsed["header"].get("dam")
    nid = PF.read_location_nid(dam_folder) or (log["settings"].get("subject_dam_nid") or
                                               "").strip() or sf.get("nid")
    v["NID_ID"] = nid if nid and nid.lower() not in ("none", "''") else None
    if log.get("failed"):           # the last attempt failed: the values are the earlier run's
        log = {"settings": log.get("settings") or {}}
    v["RUN_DATE"] = log.get("finished") or snap.get("time") or parsed["header"].get("generated")
    v["TODAY"] = time.strftime("%Y-%m-%d")
    v["PLUGIN_NAME"] = PLUGIN_NAME
    v["PLUGIN_VERSION"] = log.get("version") or parsed["header"].get("version")
    v["HYDRAULIC_MODEL"] = parsed["header"].get("model")
    v["GOVERNING_SCENARIO"] = snap.get("governing") or None
    # ---- ND
    v["ND_CLASS"] = snap.get("nd") or sf.get("nd")
    _h, nd_lines = PF.section(parsed, "ND HAZARD CLASSIFICATION")
    basis = [x for x in nd_lines if x.startswith("Basis:")]
    v["ND_BASIS"] = basis[0].split(":", 1)[1].strip() if basis else None
    reasons = sf.get("nd_reasons") or []
    if reasons:
        v["ND_GOVERNING_CRITERION"] = reasons[0]
        m = re.search(r"\[([0-9][0-9.]*[0-9])\]", reasons[0])
        v["ND_GOVERNING_SECTION"] = m.group(1) if m else None
        v["ND_REASONS"] = "; ".join(reasons)
    # ---- TM 11
    v["TM11_CLASS"] = snap.get("tm11") or sf.get("tm11")
    lij = snap.get("lij") if snap.get("lij") is not None else sf.get("lij")
    v["LIVES_IN_JEOPARDY"] = _fmt(float(lij), 1) if lij is not None else None
    for x in sf.get("tm11_lines") or []:
        if x.startswith("Without judgment-zone items:"):
            v["TM11_WITHOUT_JUDGMENT"] = x.split(":", 1)[1].strip()
    # ---- consequences
    _h, cons = PF.section(parsed, "CONSEQUENCES")
    for x in cons:
        m = re.search(r"population at risk ([\d,.]+), estimated fatalities ([\d,.]+) \(range "
                      r"([\d,.]+) - ([\d,.]+)\)", x)
        if m:
            v["PAR"], v["EST_LIFE_LOSS"] = m.group(1), m.group(2)
            v["EST_LIFE_LOSS_RANGE"] = "{} - {}".format(m.group(3), m.group(4))
        m = re.search(r"night ([\d,.]+) \(estimated fatalities ([\d,.]+)\), day ([\d,.]+) "
                      r"\(([\d,.]+)\)", x)
        if m:
            v["PAR_NIGHT"], v["EST_LIFE_LOSS_NIGHT"] = m.group(1), m.group(2)
            v["PAR_DAY"], v["EST_LIFE_LOSS_DAY"] = m.group(3), m.group(4)
    # ---- structures
    if snap:
        st = snap.get("structures") or []
        v["FLOODED_HABITABLE"] = str(sum(1 for s in st if s.get("habitable") == "Yes"
                                         and s.get("outcome") not in _DRY))
        v["CUT_OFF_HABITABLE"] = str(sum(1 for s in st if s.get("habitable") == "Yes"
                                         and s.get("cut_off") and s.get("outcome") in _DRY))
    _h, zl = PF.section(parsed, "HABITABLE STRUCTURES BY ACER ZONE")
    names = {"Habitable": "FLOODED_HABITABLE", "House on foundation": "FLOODED_HOUSES",
             "Mobile home": "FLOODED_MOBILE_HOMES"}
    for x in zl:
        m = re.match(r"^(Habitable|House on foundation|Mobile home)\s+High\s+(\d+)\s+Judgment"
                     r"\s+(\d+)\s+Low \(wet\)\s+(\d+)", x)
        if m:
            k = names[m.group(1)]
            v[k + "_HIGH"], v[k + "_JUDGMENT"], v[k + "_LOW"] = m.group(2), m.group(3), \
                m.group(4)
        m = re.match(r"^Unverified structures in flooded area:\s*(\d+)", x)
        if m:
            v["UNVERIFIED_FLOODED"] = m.group(1)
    # ---- roads, rail
    h, _l = PF.section(parsed, "ROADS:")
    if h:
        m = re.match(r"ROADS:\s*(\d+) overtopped stretch\(es\) - High (\d+), Medium (\d+)", h)
        if m:
            v["ROADS_OVERTOPPED"], v["ROADS_OVERTOPPED_HIGH"], v["ROADS_OVERTOPPED_MEDIUM"] = \
                m.group(1), m.group(2), m.group(3)
    h, _l = PF.section(parsed, "RAIL:")
    if h:
        m = re.match(r"RAIL:\s*(\d+) inundated", h)
        if m:
            v["RAIL_INUNDATED"] = m.group(1)
    gov = snap.get("governing")
    roads = PF.read_csv_rows(_table(out, "Roads_Inundated", gov) or "")
    best = None
    for r in roads:
        a = _num(r.get("AADT"))
        if a is not None and (best is None or a > best[0]):
            best = (a, r.get("Road_Name") or "unnamed road")
    if best:
        v["ROADS_MAX_AADT"] = "{:,.0f}".format(best[0])
        v["ROADS_MAX_AADT_ROAD"] = best[1]
    rails = PF.read_csv_rows(_table(out, "Rail_Inundated", gov) or "")
    if rails:
        seen = []
        for r in rails:
            t = " ".join(x for x in ((r.get("Railroad") or "").strip(),
                                     (r.get("Subdivision") or "").strip()) if x)
            if t and t not in seen:
                seen.append(t)
        v["RAIL_LINES"] = "; ".join(seen) or None
    # ---- lifelines, other dams
    if snap:
        life = snap.get("lifelines") or []
        v["LIFELINES_INUNDATED"] = str(len(life))
        v["LIFELINES_LIST"] = "; ".join(life) or None
    dams = PF.read_csv_rows(_table(out, "Dams_In_Series", gov) or "")
    if dams or os.path.isfile(os.path.join(out, SUBDIR_CLASSIFICATION, "Dams_In_Series.csv")):
        wet = [r for r in dams if (r.get("Flooded") or "") == "Yes"
               and not (r.get("Note") or "").startswith("the dam being classified")]
        v["OTHER_DAMS_INUNDATED"] = str(len(wet))
        v["OTHER_DAMS_LIST"] = "; ".join(
            "{}{}".format(r.get("Dam_Name") or "unnamed dam",
                          " ({})".format(r["NID_ID"]) if r.get("NID_ID") else "")
            for r in wet) or None
    # ---- first arrival
    first = None
    for r in PF.read_csv_rows(_table(out, "Habitable_Structures", gov) or ""):
        if r.get("Habitable") != "Yes" or (r.get("ND_Outcome") or "") in _DRY:
            continue
        a = _num(r.get("First_Arrival_hr"))
        if a is not None and (first is None or a < first[0]):
            first = (a, r.get("Struct_ID") or "")
    if first:
        v["FIRST_ARRIVAL_HR"] = "{:.2f}".format(first[0])
        v["FIRST_ARRIVAL_STRUCTURE"] = first[1] or None
    # ---- review
    rv = PF.read_csv_rows(os.path.join(out, REVIEW_FILE))
    if rv:
        names_ = []
        for r in rv:
            n = (r.get("Reviewer") or "").strip()
            if n and n not in names_:
                names_.append(n)
        v["REVIEWERS"] = ", ".join(names_) or None
        v["REVIEW_DECISIONS"] = str(len(rv))
    # ---- termination, sensitivity, vintage
    term = PF.read_csv_rows(os.path.join(qa, PF.TERMINATION_CSV))
    if term:
        t = term[0]
        rm = (t.get("River_Mile") or "").strip()
        v["TERMINATION_POINT"] = "{}{}".format(t.get("Status") or "?",
                                               " - river mile {}".format(rm) if rm else "")
        v["TERMINATION_RIVER_MILE"] = rm or None
    v["SENSITIVITY_VERDICT"] = sf.get("sensitivity")
    d, _src = PF.vintage_oldest(qa)
    v["OLDEST_DATA_DATE"] = d
    for k in list(v):
        if v[k] is not None and not isinstance(v[k], str):
            v[k] = str(v[k])
        if v[k] is not None and not v[k].strip():
            v[k] = None
    return v


def write_values_csv(output_folder, out_path=None, values=None):
    """Form_Values.csv (KEY, Value, Description) in the outputs folder (or at
    out_path). Returns its path."""
    vals = values if values is not None else available_values(output_folder)
    path = PF._free_path(out_path or os.path.join(output_folder, VALUES_CSV))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["KEY", "Value", "Description"])
        for k in vals:
            w.writerow([k, "" if vals[k] is None else vals[k], KEY_DESCRIPTIONS.get(k, "")])
    return path


def key_list_lines():
    """'{{KEY}} - description' lines (for the dialog's help / a starter template)."""
    return ["{{{{{}}}}} - {}".format(k, d) for k, d in FORM_KEYS]


# ------------------------------------------------------------------ filling --
class _Filler(object):
    def __init__(self, values, missing_text, escape=None):
        self.values = {str(k).upper(): v for k, v in (values or {}).items()}
        self.missing_text = missing_text
        self.escape = escape or (lambda s: s)
        self.unknown, self.missing = set(), set()

    def value_for(self, name, original):
        """Replacement text for {{name}} (raw, not escaped), or None = leave it."""
        key = name.upper()
        if key not in self.values:
            self.unknown.add(key)
            return None
        val = self.values[key]
        if val is None or str(val) == "":
            self.missing.add(key)
            return None if self.missing_text is None else str(self.missing_text)
        return str(val)

    def sub(self, text):
        def rep(m):
            got = self.value_for(m.group(1), m.group(0))
            return m.group(0) if got is None else self.escape(got)
        return PLACEHOLDER_RE.sub(rep, text)


def _read_template_text(path):
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc), bom, enc
        except UnicodeDecodeError:
            continue
    raise ValueError("{} is not a text file".format(path))


def _fill_csv(text, filler):
    first = text.splitlines()[0] if text.strip() else ""
    delim = ";" if first.count(";") > first.count(",") else ","
    import io
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    out = io.StringIO()
    w = csv.writer(out, delimiter=delim, lineterminator="\r\n")
    for r in rows:
        w.writerow([filler.sub(c) for c in r])
    return out.getvalue()


# ---- .docx ------------------------------------------------------------------
_DOCX_PARTS = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
_TOKEN = re.compile(r"<w:p(?=[\s>/])[^>]*?(/?)>|(</w:p>)|<w:t(?=[\s>])([^>]*)>([^<]*)</w:t>")


def _unescape_xml(s):
    return html.unescape(s)


def fill_docx_xml(xml, filler):
    """The XML of one Word part with the placeholders of each paragraph
    replaced (see the module docstring). Returns (new xml, changed?)."""
    stack, next_id = [], 0
    nodes = []                  # [para id, match start, match end, attrs, text]
    for m in _TOKEN.finditer(xml):
        if m.group(0).startswith("<w:p"):
            if m.group(1) != "/":
                stack.append(next_id)
                next_id += 1
        elif m.group(2):
            if stack:
                stack.pop()
        else:
            nodes.append([stack[-1] if stack else -1, m.start(), m.end(), m.group(3),
                          _unescape_xml(m.group(4))])
    by_para = collections.OrderedDict()
    for i, n in enumerate(nodes):
        by_para.setdefault(n[0], []).append(i)
    new_text = {}
    for _pid, idxs in by_para.items():
        texts = [nodes[i][4] for i in idxs]
        full = "".join(texts)
        if "{{" not in full:
            continue
        matches = list(PLACEHOLDER_RE.finditer(full))
        if not matches:
            continue
        changed = False
        for m in reversed(matches):
            rep = filler.value_for(m.group(1), m.group(0))
            if rep is None:
                continue
            rep = rep.replace("\r\n", "; ").replace("\n", "; ")
            s, e = m.start(), m.end()
            offs, o = [], 0
            for t in texts:
                offs.append(o)
                o += len(t)
            # the run where the placeholder starts (i) and the one where it ends (j)
            i = next(k for k in range(len(texts)) if offs[k] <= s < offs[k] + len(texts[k]))
            j = next(k for k in range(len(texts))
                     if offs[k] <= e - 1 < offs[k] + len(texts[k]))
            if i == j:
                t = texts[i]
                texts[i] = t[:s - offs[i]] + rep + t[e - offs[i]:]
            else:
                texts[i] = texts[i][:s - offs[i]] + rep
                for k in range(i + 1, j):
                    texts[k] = ""
                texts[j] = texts[j][e - offs[j]:]
            changed = True
        if changed:
            for k, i in enumerate(idxs):
                if texts[k] != nodes[i][4]:
                    new_text[i] = texts[k]
    if not new_text:
        return xml, False
    out, last = [], 0
    for i in sorted(new_text):
        _p, start, end, attrs, _old = nodes[i]
        t = new_text[i]
        if t and (t != t.strip() or "  " in t) and "xml:space" not in attrs:
            attrs = attrs + ' xml:space="preserve"'
        out.append(xml[last:start])
        out.append("<w:t{}>{}</w:t>".format(attrs, _xml_escape(t)))
        last = end
    out.append(xml[last:])
    return "".join(out), True


def _fill_docx(template_path, out_path, filler):
    with zipfile.ZipFile(template_path) as zin:
        infos = zin.infolist()
        if "word/document.xml" not in {i.filename for i in infos}:
            raise ValueError("{} is not a Word document (no word/document.xml)".format(
                template_path))
        tmp = out_path + ".tmp"
        with zipfile.ZipFile(tmp, "w") as zout:
            for info in infos:
                data = zin.read(info.filename)
                if _DOCX_PARTS.match(info.filename):
                    xml = data.decode("utf-8")
                    new, changed = fill_docx_xml(xml, filler)
                    if changed:
                        data = new.encode("utf-8")
                zout.writestr(info, data, compress_type=info.compress_type)
    os.replace(tmp, out_path)


def placeholders_in(template_path):
    """Sorted KEYs of the placeholders a template holds (a .docx: per paragraph,
    across Word's runs)."""
    names = set()
    ext = os.path.splitext(template_path)[1].lower()
    if ext == ".docx":
        with zipfile.ZipFile(template_path) as z:
            for n in z.namelist():
                if _DOCX_PARTS.match(n):
                    xml = z.read(n).decode("utf-8")
                    probe = _Filler({}, None)
                    fill_docx_xml(xml, probe)
                    names |= probe.unknown
        return sorted(names)
    text, _b, _e = _read_template_text(template_path)
    return sorted({m.group(1).upper() for m in PLACEHOLDER_RE.finditer(text)})


def fill_template(template_path, values, out_path, missing_text=None):
    """Fill a .txt / .md / .html / .csv / .docx template's {{KEY}} placeholders
    with `values` (available_values() or any dict; KEYs are not case-
    sensitive). Returns (out_path actually written, unknown placeholders (no
    such KEY in values), missing values (KEY known, no value)). Unknown and
    value-less placeholders stay in the output unless `missing_text` is given
    (it then replaces the value-less ones). HTML values are escaped; a CSV is
    filled cell by cell (values with commas are quoted); a .docx keeps its
    formatting (see the module docstring). Raises ValueError for another file
    type or a broken template."""
    if not template_path or not os.path.isfile(template_path):
        raise ValueError("Template not found: {}".format(template_path))
    ext = os.path.splitext(template_path)[1].lower()
    if os.path.normcase(os.path.abspath(template_path)) == os.path.normcase(
            os.path.abspath(out_path)):
        raise ValueError("Write the filled form to another file than the template")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    out_path = PF._free_path(out_path)
    if ext == ".docx":
        filler = _Filler(values, missing_text)
        try:
            _fill_docx(template_path, out_path, filler)
        except zipfile.BadZipFile:
            raise ValueError("{} is not a valid .docx file".format(template_path))
    elif ext in TEXT_EXTS + HTML_EXTS + (".csv",):
        text, bom, enc = _read_template_text(template_path)
        filler = _Filler(values, missing_text,
                         (lambda s: html.escape(s, quote=True)) if ext in HTML_EXTS else None)
        new = _fill_csv(text, filler) if ext == ".csv" else filler.sub(text)
        with open(out_path, "w", encoding="utf-8-sig" if (bom or ext == ".csv") else "utf-8",
                  newline="" if ext == ".csv" else None) as fh:
            fh.write(new)
    else:
        raise ValueError("Template type {} is not supported - use .docx, .txt, .md, .html or "
                         ".csv (for a PDF form, copy the values from {})".format(
                             ext or "(none)", VALUES_CSV))
    return out_path, sorted(filler.unknown), sorted(filler.missing)
