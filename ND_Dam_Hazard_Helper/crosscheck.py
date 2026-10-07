# -*- coding: utf-8 -*-
"""Cross-check of the plugin's population at risk against the human
consequence table (HCOM) DSS-WISE Lite produces for the same flood.

DSS-WISE counts people by census block (and, with HCOM, day and night
population and housing units); the plugin counts them structure by
structure (NSI populations, ND address points, building outlines, TM 11
persons per dwelling). The two are different estimates of the same thing,
so a large difference is not an error by itself - it is a reason to look:
a missing structure layer, a census block partly outside the model, a
campground, a change of use. The check compares like with like (night with
night, day with day) and says how far apart they are; it does not change
the classification.
"""

import csv
import os

from .helpers import prepare_output_path

# ratio (plugin / DSS-WISE) inside which the two are called consistent
RATIO_LOW, RATIO_HIGH = 0.75, 1.0 / 0.75
CSV_NAME = "DSSWISE_HCOM_CrossCheck.csv"
HEAD = ["HCOM_table", "Quantity", "HCOM_value", "HCOM_column", "Plugin_value",
        "Plugin_quantity", "Ratio_plugin_to_HCOM", "Verdict"]


def split_files(text):
    """HCOM file paths from the dialog / settings text (';' separated)."""
    if isinstance(text, (list, tuple)):
        return [str(t).strip() for t in text if str(t).strip()]
    return [t.strip() for t in str(text or "").split(";") if t.strip()]


def verdict(hcom, plugin):
    """(ratio or None, text) for one pair of numbers."""
    if hcom is None or plugin is None:
        return None, "not compared"
    if hcom <= 0 and plugin <= 0:
        return None, "agree (both zero)"
    if hcom <= 0:
        return None, "the plugin counts people where DSS-WISE counts none - check"
    r = plugin / hcom
    if RATIO_LOW <= r <= RATIO_HIGH:
        return r, "consistent"
    if r > RATIO_HIGH:
        return r, "plugin higher ({:.1f}x) - check for structures outside DSS-WISE's " \
            "census blocks, campgrounds, or a persons-per-dwelling that is too high".format(r)
    return r, "plugin lower ({:.1f}x of DSS-WISE) - check for missing structure data " \
        "(NSI / address / outline layers), or census blocks that reach past the flood".format(r)


def pairs(tot, conseq, population="night"):
    """[(quantity, hcom key, plugin quantity label, plugin value)] to compare,
    like with like: night with night, day with day; census population with
    the night population; a DSS-WISE population at risk with the setting's."""
    out = []
    if "pop_night" in tot:
        out.append(("population (night)", "pop_night", "population at risk, night",
                    conseq.get("par_night")))
    if "pop_day" in tot:
        out.append(("population (day)", "pop_day", "population at risk, day",
                    conseq.get("par_day")))
    if "pop_night" not in tot and "pop_day" not in tot and "pop" in tot:
        out.append(("population", "pop", "population at risk, night", conseq.get("par_night")))
    if "par" in tot:
        out.append(("population at risk", "par",
                    "population at risk, {}".format(population), conseq.get("par")))
    return out


def compare(tables, conseq, population="night"):
    """(lines, rows) for HCOM totals (dsswise.hcom_totals dicts) against the
    plugin's consequence totals (consequences.totals)."""
    lines, rows = [], []
    for tot in tables:
        if tot.get("error"):
            lines.append("HCOM table {}: {} - not compared".format(tot["file"], tot["error"]))
            continue
        prs = pairs(tot, conseq, population)
        if not prs:
            lines.append("HCOM table {}: no population column recognised (columns: {}) - not "
                         "compared".format(tot["file"], ", ".join(tot.get("header", [])[:12])))
            continue
        for qty, key, plabel, pval in prs:
            r, text = verdict(tot.get(key), pval)
            rows.append({"HCOM_table": tot["file"], "Quantity": qty,
                         "HCOM_value": tot.get(key), "HCOM_column": tot["columns"].get(key, ""),
                         "Plugin_value": pval, "Plugin_quantity": plabel,
                         "Ratio_plugin_to_HCOM": None if r is None else round(r, 2),
                         "Verdict": text})
            lines.append("{} - {}: DSS-WISE {:,.0f} (column '{}') against the plugin's {:,.0f} "
                         "({}): {}".format(tot["file"], qty, tot.get(key) or 0,
                                           tot["columns"].get(key, "?"), pval or 0, plabel,
                                           text))
        if "housing" in tot:
            lines.append("{}: DSS-WISE housing units {:,.0f} (whole table; the plugin's "
                         "flooded habitable structures are counted per structure)".format(
                             tot["file"], tot["housing"]))
    if lines:
        lines.append("The two count people differently (census blocks against structures); "
                     "the check does not change the classification")
    return lines, rows


def write_csv(folder, rows):
    """QA/DSSWISE_HCOM_CrossCheck.csv; returns the path."""
    path = prepare_output_path(os.path.join(folder, CSV_NAME))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=HEAD)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in HEAD})
    return path
