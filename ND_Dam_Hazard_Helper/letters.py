# -*- coding: utf-8 -*-
"""2.30.0: the dam-owner letter and the county notification, as editable drafts.

After a classification the engineer writes the same two letters every time: one to the dam
owner (this is your class, this is why, this is what is downstream, this is what changed since
the last classification) and one to the county emergency manager (what is in the flood, how
much warning time there is, where the inundation maps and the notification list are). This
module drafts both from the run's own result so the engineer edits instead of types.

    facts = facts_from(res, card=card, history=entries, lifelines=rows)
    paras = owner_letter(facts)            # [(style, text)]
    write_md(path, title, paras)           # or write_docx(path, title, paras)

Decisions:
  * NO LEGAL LANGUAGE IS INVENTED. The drafts state facts of the run and quote the ND policy
    reference; everything that is a notice, a duty, a deadline or an instruction to the owner
    is left as a square-bracket placeholder for the engineer. A plugin cannot know what a
    letter has to say to be a notice under the standards, so it does not pretend to.
  * Every draft carries DRAFT in its title, in its first paragraph and in its footer, and says
    which plugin and version produced it from which run - so a copy that escapes the engineer's
    desk cannot be mistaken for the signed letter.
  * A number that the run does not have becomes a placeholder, never a zero: "[warning time]"
    is honest, "0 minutes" is not.
  * .docx is written as the minimal Office Open XML a Word / LibreOffice reader accepts (a zip
    with [Content_Types].xml, _rels/.rels and word/document.xml) - the same approach as
    damsuite.xlsx_lite, so the plugin needs no python-docx. The editable source is the plain
    text the window shows; .md and .docx are two renderings of it.
  * Pure Python standard library: nothing here needs QGIS, so it can be tested.
"""

import os
import re
import time
import zipfile

DRAFT_MARK = "DRAFT - for the engineer of record to review, complete and sign"
OWNER_TITLE = "Dam owner letter (draft)"
COUNTY_TITLE = "County emergency manager notification (draft)"
# (style, text): "h" a heading, "p" a paragraph, "b" a bullet, "" a blank line
H, P, B, BLANK = "h", "p", "b", ""
PLACEHOLDER = re.compile(r"\[[^\]]+\]")


def _s(v, default=""):
    v = "" if v is None else str(v).strip()
    return v or default


def _n(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def facts_from(res=None, card=None, history=(), lifelines=(), dam_name="", nid_id="",
               policy_ref="", plugin="", version="", when=None):
    """The facts both letters are built from - a plain dict the caller can edit before
    drafting (the window lets the engineer do exactly that)."""
    res = res or {}
    card = card or {}
    nd = res.get("nd") or {}
    tm11 = res.get("tm11") or {}
    counts = nd.get("counts") or {}
    acc = res.get("access") or {}
    runs = list(history or ())
    changed = ""
    if len(runs) >= 2 and runs[-1].get("nd") != runs[-2].get("nd"):
        changed = "{} -> {} (run of {})".format(runs[-2].get("nd"), runs[-1].get("nd"),
                                                _s(runs[-1].get("time"), "?"))
    return {
        "dam": _s(dam_name or res.get("dam_name") or card.get("dam"), "[dam name]"),
        "nid_id": _s(nid_id, "[NID ID]"),
        "date": when or time.strftime("%B %d, %Y"),
        "cls": _s(card.get("cls") or nd.get("cls"), "[class]"),
        "provisional": bool(card.get("provisional") or nd.get("provisional")),
        "reason": _s(card.get("reason"), "[the reason for the class]"),
        "change": _s(card.get("change"), "[what would change the class]"),
        "flags": list(card.get("flags") or ()),
        "tm11": _s(tm11.get("cls"), "[TM 11 class]"),
        "lij": tm11.get("lij"),
        "governing": _s(res.get("governing"), "[scenario]"),
        "structures": _n(counts.get("habitable_inundated")),
        "probable": _n(counts.get("habitable_probable")),
        "people": _n(counts.get("persons_probable")),
        "roads": _n(counts.get("roads_high")) + _n(counts.get("roads_medium")),
        "rail": _n(counts.get("rail")),
        "lifeline_count": _n(counts.get("lifeline")),
        "access_cut": _n(acc.get("cut")),
        "lifelines": [_s(r.get("line") or r.get("name")) for r in lifelines or ()
                      if _s(r.get("line") or r.get("name"))],
        "history_line": _s(card.get("history")),
        "class_change": changed,
        "runs": len(runs),
        "policy": _s(policy_ref, "the North Dakota Dam Safety Standards"),
        "plugin": _s(plugin, "ND Dam Hazard Classification Helper"),
        "version": _s(version),
        "outputs": _s(res.get("output_folder")),
    }


def _downstream_bullets(f):
    out = []
    if f["structures"]:
        out.append("{} habitable structure(s) are inundated; {} of them are in flood "
                   "conditions the ACER TM 11 curves call lethal ({} people)".format(
                       f["structures"], f["probable"], f["people"]))
    else:
        out.append("no habitable structure is inundated in the modelled breach")
    if f["access_cut"]:
        out.append("{} habitable structure(s) lose their only road access, including dry "
                   "ones".format(f["access_cut"]))
    if f["roads"]:
        out.append("{} road stretch(es) are overtopped".format(f["roads"]))
    if f["rail"]:
        out.append("{} railroad stretch(es) are inundated".format(f["rail"]))
    if f["lifeline_count"]:
        out.append("{} lifeline / critical facilit(ies) are inundated".format(
            f["lifeline_count"]))
    for line in f["lifelines"][:5]:
        out.append(line)
    return out


def _footer(f):
    return ("{} - produced by {}{} from the classification run of the '{}' scenario. The "
            "screening results in this draft are not the hazard classification until the "
            "engineer of record adopts them.".format(
                DRAFT_MARK, f["plugin"], " v" + f["version"] if f["version"] else "",
                f["governing"]))


def owner_letter(facts):
    """[(style, text)] of the dam-owner draft."""
    f = dict(facts or {})
    cls = f.get("cls", "[class]")
    paras = [
        (H, OWNER_TITLE),
        (P, DRAFT_MARK + "."),
        (P, "[Engineering firm letterhead]"),
        (P, f.get("date", "")),
        (P, "[Dam owner name]\n[Address]"),
        (P, "RE: Hazard potential classification - {} (NID ID {})".format(
            f.get("dam"), f.get("nid_id"))),
        (P, "Dear [owner]:"),
        (P, "We have completed a dam-break analysis and hazard potential classification "
            "screening for {} under {}. The screening result is a {} hazard potential "
            "classification{}.".format(f.get("dam"), f.get("policy"), cls.upper(),
                                       " (provisional)" if f.get("provisional") else "")),
        (H, "Why the class is {}".format(cls)),
        (P, f.get("reason", "")),
        (P, "What would change it: {}".format(f.get("change", ""))),
        (H, "What is downstream of the dam in the modelled breach"),
    ]
    paras += [(B, b) for b in _downstream_bullets(f)]
    paras.append((H, "What has changed since the last classification"))
    if f.get("class_change"):
        paras.append((P, "The class changed: {}. [State why: new data, a new model, new "
                         "development downstream, or a changed rule.]".format(
                             f["class_change"])))
    elif f.get("history_line"):
        paras.append((P, f["history_line"] + ". [State what was re-run and why.]"))
    else:
        paras.append((P, "This is the first classification run recorded for this dam in the "
                         "plugin's log. [State the previous classification on record and its "
                         "date, if any.]"))
    if f.get("flags"):
        paras.append((H, "Qualifications on this result"))
        paras += [(B, x) for x in f["flags"]]
    paras += [
        (H, "What happens next"),
        (P, "[The engineer states here what the classification means for the owner under the "
            "standards, what the owner is asked to do, and by when. Do not use this draft "
            "without completing this section.]"),
        (P, "Enclosed: hazard classification report, ACER TM 11 and ND 5.2 criteria tables, "
            "inundation maps and the EAP notification list. [Adjust the enclosures.]"),
        (P, "Sincerely,\n\n[Name, P.E.]\n[License no. / state]\n[Telephone / e-mail]"),
        (P, _footer(f)),
    ]
    return paras


def county_letter(facts):
    """[(style, text)] of the county notification draft."""
    f = dict(facts or {})
    paras = [
        (H, COUNTY_TITLE),
        (P, DRAFT_MARK + "."),
        (P, "[Engineering firm letterhead]"),
        (P, f.get("date", "")),
        (P, "[County emergency manager]\n[County] County Emergency Management\n[Address]"),
        (P, "RE: Dam-break inundation information - {} (NID ID {})".format(
            f.get("dam"), f.get("nid_id"))),
        (P, "Dear [emergency manager]:"),
        (P, "A dam-break analysis has been completed for {}. The screening hazard potential "
            "classification is {}. This letter passes on what the analysis found downstream, "
            "for your emergency planning; it is not a warning of a current "
            "condition.".format(f.get("dam"), f.get("cls", "[class]").upper())),
        (H, "What the modelled breach reaches"),
    ]
    paras += [(B, b) for b in _downstream_bullets(f)]
    paras += [
        (H, "Warning time and notification"),
        (P, "The EAP notification list that comes with the report lists every place to warn in "
            "the order the flood reaches it, with the arrival time at each. [State the first "
            "arrival time and the places with the least warning time from that list.]"),
        (H, "Maps"),
        (P, "The inundation maps (and the GeoPDF versions, which carry coordinates for use in "
            "the field) shade the flooded area by arrival time and show the structures, roads "
            "and lifeline facilities in it. [Name what is enclosed or where it is shared.]"),
        (H, "What we ask of you"),
        (P, "[The engineer states here what the county is asked to do - review the notification "
            "list, confirm the facilities and the routes, or nothing at all. Do not use this "
            "draft without completing this section.]"),
        (P, "Sincerely,\n\n[Name, P.E.]\n[Telephone / e-mail]"),
        (P, _footer(f)),
    ]
    if f.get("flags"):
        paras.insert(len(paras) - 2, (H, "Qualifications on this result"))
        for x in f["flags"]:
            paras.insert(len(paras) - 2, (B, x))
    return paras


DRAFTS = (("owner", OWNER_TITLE, owner_letter), ("county", COUNTY_TITLE, county_letter))


def draft(kind, facts):
    """(title, paragraphs) for "owner" or "county"."""
    for key, title, fn in DRAFTS:
        if key == kind:
            return title, fn(facts)
    raise ValueError("unknown draft '{}' - use 'owner' or 'county'".format(kind))


def placeholders(paras):
    """Every [placeholder] still in the draft - what the engineer has to fill in."""
    out = []
    for _style, text in paras or ():
        for m in PLACEHOLDER.findall(text or ""):
            if m not in out:
                out.append(m)
    return out


# ------------------------------------------------------------- plain text / md --
def to_text(paras):
    """The editable plain text the window shows ("# " heading, "- " bullet)."""
    out = []
    for style, text in paras or ():
        if style == H:
            out.append("")
            out.append("# " + (text or ""))
        elif style == B:
            out.append("- " + (text or ""))
        elif style == BLANK:
            out.append("")
        else:
            out.append(text or "")
        if style in (H, P):
            out.append("")
    return "\n".join(out).strip() + "\n"


def from_text(text):
    """The paragraphs of edited text again (the inverse of to_text, for the exporters)."""
    paras = []
    for block in re.split(r"\n\s*\n", str(text or "")):
        block = block.strip("\n")
        if not block.strip():
            continue
        for line in block.split("\n") if block.lstrip().startswith(("- ", "# ")) else [block]:
            t = line.strip()
            if t.startswith("# "):
                paras.append((H, t[2:].strip()))
            elif t.startswith("- "):
                paras.append((B, t[2:].strip()))
            elif t:
                paras.append((P, line))
    return paras


def to_markdown(title, paras):
    out = ["# {}".format(title or OWNER_TITLE), ""]
    for style, text in paras or ():
        if style == H:
            out += ["## " + (text or ""), ""]
        elif style == B:
            out.append("- " + (text or "").replace("\n", " "))
        elif style == BLANK:
            out.append("")
        else:
            out += [(text or "").replace("\n", "  \n"), ""]
    return "\n".join(out).rstrip() + "\n"


def write_md(path, title, paras):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(to_markdown(title, paras))
    return path


# --------------------------------------------------------------------- .docx --
_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _xml_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _runs(text):
    """Word runs for one paragraph: a line break inside it stays a line break."""
    parts = str(text or "").split("\n")
    out = []
    for i, part in enumerate(parts):
        if i:
            out.append("<w:r><w:br/></w:r>")
        out.append("<w:r><w:t xml:space=\"preserve\">{}</w:t></w:r>".format(_xml_escape(part)))
    return "".join(out)


def _paragraph(style, text, title=False):
    if title or style == H:
        size = "32" if title else "26"
        pr = "<w:pPr><w:spacing w:before=\"200\" w:after=\"80\"/><w:rPr><w:b/>" \
             "<w:sz w:val=\"{}\"/></w:rPr></w:pPr>".format(size)
        runs = "<w:r><w:rPr><w:b/><w:sz w:val=\"{}\"/></w:rPr><w:t xml:space=\"preserve\">" \
               "{}</w:t></w:r>".format(size, _xml_escape(str(text or "").replace("\n", " ")))
        return "<w:p>{}{}</w:p>".format(pr, runs)
    if style == B:
        return "<w:p><w:pPr><w:ind w:left=\"360\"/></w:pPr>{}</w:p>".format(
            _runs(u"• " + str(text or "")))
    return "<w:p><w:pPr><w:spacing w:after=\"120\"/></w:pPr>{}</w:p>".format(_runs(text))


def to_docx_xml(title, paras):
    body = [_paragraph(H, title, title=True)]
    body += [_paragraph(style, text) for style, text in paras or ()
             if not (style == H and (text or "") == title)]
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
            "<w:document xmlns:w=\"{}\"><w:body>{}"
            "<w:sectPr><w:pgSz w:w=\"12240\" w:h=\"15840\"/>"
            "<w:pgMar w:top=\"1440\" w:right=\"1440\" w:bottom=\"1440\" w:left=\"1440\"/>"
            "</w:sectPr></w:body></w:document>".format(_W, "".join(body)))


def write_docx(path, title, paras):
    """A minimal Word document (no python-docx). Returns the path."""
    xml = to_docx_xml(title, paras)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", xml)
    return path


def write_draft(folder, kind, facts, fmt="md", paras=None, name=None):
    """Write one draft into `folder` as .md or .docx. Returns the path."""
    title, built = draft(kind, facts)
    paras = built if paras is None else paras
    base = name or "{}_Letter_Draft".format("Dam_Owner" if kind == "owner" else "County")
    path = os.path.join(folder, base + (".docx" if fmt == "docx" else ".md"))
    if fmt == "docx":
        return write_docx(path, title, paras)
    return write_md(path, title, paras)
