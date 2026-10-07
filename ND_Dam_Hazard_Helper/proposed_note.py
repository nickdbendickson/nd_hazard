"""The proposed-dam sentence, carried into the hazard summary and the PDF.

Screening writes it on dam.json (extra["proposed_embankment"]). This plugin does not
rebuild the embankment; it only repeats the sentence the other two plugins use.
"""
import os

PHRASE = "proposed — embankment inserted from the drawn line"


def find_record(folder):
    """dam.json in `folder` or up to four parents (the dam folder may be the parent
    of the classification output). None when there is none or it cannot be read."""
    if not folder:
        return None
    here = os.path.abspath(folder)
    for _i in range(5):
        path = os.path.join(here, "dam.json")
        if os.path.isfile(path):
            try:
                from .damsuite import dam_record as dr
                return dr.load(here)
            except Exception:
                return None
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return None


def lines_for(record):
    """Sentences for the summary, or [] when this dam was not proposed."""
    if record is None:
        return []
    spec = (record.extra or {}).get("proposed_embankment")
    if not isinstance(spec, dict):
        return []
    phrase = spec.get("phrase") or ""
    if PHRASE not in phrase and PHRASE not in str(record.extra):
        return []
    out = [phrase or PHRASE]
    kind = spec.get("rim_kind")
    if kind and kind != "contained":
        rim = spec.get("rim_m")
        extra = ""
        if isinstance(rim, (int, float)):
            extra = " (rim {:.1f} ft)".format(float(rim) / 0.3048)
        out.append("The pool leaves the basin ({}) below the design crest{}.".format(
            str(kind).replace("_", " "), extra))
    return out
