# -*- coding: utf-8 -*-
"""v2.9: 'Review: roads & facilities' tab - the engineer's decisions on the
overtopped road stretches, the inundated railroad stretches, the bridges /
culverts / low-water crossings and the lifeline facilities of the last
Hazard Classification run. The decisions are saved to
outputs/Facility_Review_Decisions.csv and applied on every later run
(facility_review.py): an excluded item is left out of the classification
and the output layers, a set AADT / trains per day replaces the automatic
value, a set category replaces the lifeline category.

The lists come from the last run (self._last_run, the dict the pipeline
returns): its "results" (per scenario: "roads", "rails",
"facility_records", "fac_eval", "fac_charged", "scenario", and what
facility_review.apply_facility_decisions kept), "governing", "crs" and
"facility_review_file" (else the outputs folder of "review_file").

The table is sorted by clicking a column heading (again: the other way);
the sorting is done here, not by the table (its cells hold widgets). Edits
not saved yet are kept when the kind, the scenario, the sorting or the
'List every item' option changes."""

import math
import os
import re

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QWidget, QTableWidget, QTableWidgetItem,
    QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QMessageBox, QCheckBox,
)
from qgis.core import QgsProject, QgsCoordinateTransform, QgsRectangle

from .helpers import log_message, buffer_distance_in_layer_units
from . import facility_review as FR

TAB_TITLE = "Review: roads & facilities"

_KEEP = "(no change)"
_CLEAR = "(remove saved decision)"
_CONFIRM = "Automatic result is correct"
# (label, decision saved in the file)
DECISION_CHOICES = {
    FR.KIND_ROAD: [(_KEEP, None), (_CONFIRM, FR.DEC_CONFIRMED),
                   ("Exclude (not a public road, private drive, not overtopped...)",
                    FR.DEC_EXCLUDED),
                   ("Set AADT (number at the right)", FR.DEC_SET_AADT)],
    FR.KIND_RAIL: [(_KEEP, None), (_CONFIRM, FR.DEC_CONFIRMED),
                   ("Exclude (abandoned, out of service, not a railroad...)", FR.DEC_EXCLUDED),
                   ("Set trains per day (number at the right)", FR.DEC_SET_TRAINS)],
    FR.KIND_CROSSING: [(_KEEP, None), (_CONFIRM, FR.DEC_CONFIRMED),
                       ("Exclude (not a crossing, wrong data...)", FR.DEC_EXCLUDED),
                       ("Overtopped: yes", FR.DEC_OVERTOPPED_YES),
                       ("Overtopped: no (deck above the water)", FR.DEC_OVERTOPPED_NO)],
    FR.KIND_LIFELINE: [(_KEEP, None), (_CONFIRM, FR.DEC_CONFIRMED),
                       ("Not a lifeline facility (exclude)", FR.DEC_EXCLUDED),
                       ("Set category (at the right)", FR.DEC_SET_CATEGORY)],
}
VALUE_HEADER = {FR.KIND_ROAD: "Set AADT", FR.KIND_RAIL: "Set trains/day",
                FR.KIND_CROSSING: "-", FR.KIND_LIFELINE: "Set category"}
TRAILING = ["Reviewed", "Decision", None, "Note"]      # None = VALUE_HEADER[kind]


def _qt_sort_order(name):
    """Qt.SortOrder member (scoped spelling first, legacy Qt5 second; looked
    up by name - compat.py has no shim for it)."""
    for get in (lambda: getattr(getattr(Qt, "SortOrder"), name), lambda: getattr(Qt, name)):
        try:
            v = get()
        except Exception:
            continue
        if v is not None:
            return v
    return None


SORT_ASC = _qt_sort_order("AscendingOrder")
SORT_DESC = _qt_sort_order("DescendingOrder")


def _f(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _t(v, fmt="{:.1f}"):
    f = _f(v)
    return "" if f is None else fmt.format(f)


def _txt(s):
    s = str(s or "").strip()
    return s.casefold() if s else None


def _yes_no(v):
    return "" if v is None else ("Yes" if v else "No")


def _charged(res, rec):
    ch = (res.get("fac_charged") or {}).get(rec.get("id"))
    return "" if ch is None or not res.get("incremental") else ("Yes" if ch[0] else "No")


def _num_cell(v, fmt="{:.1f}"):
    return (_t(v, fmt), _f(v))


def _text_cell(s):
    return (str(s or ""), _txt(s))


def columns(kind):
    """Headings of the info columns of a kind (the decision columns follow)."""
    if kind == FR.KIND_ROAD:
        return ["ID", "Name", "Road class", "AADT (auto)", "AADT year / source", "Length ft",
                "Max depth ft", "Max vel ft/s", "Vehicle zone", "Charged", "ND class",
                "Why review"]
    if kind == FR.KIND_RAIL:
        return ["ID", "Railroad / subdivision", "Passenger", "Trains/day (auto)",
                "Trains source", "Length ft", "Max depth ft", "Max vel ft/s", "Charged",
                "ND class", "Why review"]
    if kind == FR.KIND_CROSSING:
        return ["ID", "Name / carries", "Category", "Structure no.", "Crosses", "ADT",
                "Flooded", "Max depth ft", "Charged", "Source", "Why review"]
    return ["ID", "Name", "Category (auto)", "Lifeline", "Detail", "Flooded", "Max depth ft",
            "Charged", "Source", "Why review"]


def _id_cell(key):
    m = re.search(r"(\d+)$", key or "")
    return (key, float(m.group(1)) if m else _txt(key))


def info_cells(res, kind, key, rec, eff=None):
    """[(text, sort value)] of the info columns (sort value None = empty):
    the automatic values of `rec`; "Charged" and "ND class" of `eff`, the
    record the last run classified (None: left out by the review)."""
    idn = _id_cell(key)
    why = "; ".join(FR.review_flags(res, kind, rec))
    if eff is None:
        charged = ""                            # left out: not part of the increment
    elif kind in FR.LINE_KINDS:
        inc = eff.get("incremental")
        charged = "" if inc is None or not res.get("incremental") else _yes_no(inc)
    else:
        charged = _charged(res, eff)
    nd_class = (eff if eff is not None else rec).get("nd_class")
    if kind == FR.KIND_ROAD:
        src = ", ".join(x for x in (str(rec["aadt_year"]) if rec.get("aadt_year") else "",
                                    rec.get("aadt_source") or "") if x)
        vz = rec.get("vehicle_zone")
        return [idn, _text_cell(rec.get("name")), _text_cell(rec.get("road_class")),
                _num_cell(rec.get("aadt"), "{:,.0f}"), _text_cell(src),
                _num_cell(rec.get("length_ft"), "{:.0f}"), _num_cell(rec.get("max_depth")),
                _num_cell(rec.get("max_vel")),
                (FR.zone_label(vz), {1: 0, 2: 1, 3: 2}.get(vz)), _text_cell(charged),
                _text_cell(nd_class), _text_cell(why)]
    if kind == FR.KIND_RAIL:
        return [idn, _text_cell(FR.item_name(kind, rec)),
                _text_cell(("Yes ({})".format(rec.get("passenger_value") or "")
                            if rec.get("passenger") else "No")),
                _num_cell(rec.get("trains"), "{:g}"), _text_cell(rec.get("trains_source")),
                _num_cell(rec.get("length_ft"), "{:.0f}"), _num_cell(rec.get("max_depth")),
                _num_cell(rec.get("max_vel")), _text_cell(charged),
                _text_cell(nd_class), _text_cell(why)]
    depth = FR.flooded_depth(res, kind, rec)
    flooded = "Yes" if depth is not None else "No"
    if kind == FR.KIND_CROSSING:
        return [idn, _text_cell(rec.get("name")), _text_cell(rec.get("category")),
                _text_cell(rec.get("struct_no")), _text_cell(rec.get("crosses")),
                _num_cell(rec.get("adt"), "{:,.0f}"), _text_cell(flooded), _num_cell(depth),
                _text_cell(charged), _text_cell(rec.get("source")), _text_cell(why)]
    return [idn, _text_cell(rec.get("name")), _text_cell(rec.get("category")),
            _text_cell("Yes" if rec.get("lifeline") else "No"), _text_cell(rec.get("detail")),
            _text_cell(flooded), _num_cell(depth), _text_cell(charged),
            _text_cell(rec.get("source")), _text_cell(why)]


def saved_triple(kind, row):
    """(decision, value, note) the widgets of a `kind` row show for a saved
    file row: (None, None, "") without one; a note-only row, or a decision
    the kind has no choice for, shows no decision; values at the widgets'
    precision (whole AADT, trains to 0.1)."""
    if row is None:
        return (None, None, "")
    d = FR.decision_of(row)
    dec = d["decision"]
    if dec not in [code for _l, code in DECISION_CHOICES[kind] if code is not None]:
        dec = None
    value = d["value"]
    if kind == FR.KIND_ROAD:
        value = None if _f(value) is None else float(int(round(float(value))))
    elif kind == FR.KIND_RAIL:
        value = None if _f(value) is None else round(float(value), 1)
    elif kind != FR.KIND_LIFELINE:
        value = None
    return (dec, value, d["note"])


def to_decision(kind, dec, value, note):
    """What saving the widget values means: (decision, value) - decision
    _CLEAR (remove), None (nothing to save) or a decision of the file; a
    text starting with '!' is a problem (not saved)."""
    if dec == _CLEAR:
        return _CLEAR, None
    vdec = FR.VALUE_DECISION.get(kind)
    if dec is None:
        if value is not None and vdec:
            dec = vdec
        elif note:
            dec = FR.DEC_NOTE
        else:
            return None, None
    if dec == vdec:
        if value is None:
            return "!{} needs a value".format(dec), None
        return dec, value
    return dec, None


def _same(row, dec, value, note):
    if row is None:
        return False
    d = FR.decision_of(row)
    if d["decision"] != dec or (d["note"] or "") != (note or ""):
        return False
    if dec in (FR.DEC_SET_AADT, FR.DEC_SET_TRAINS):
        return d["value"] is not None and value is not None and \
            abs(float(d["value"]) - float(value)) < 1e-9
    if dec == FR.DEC_SET_CATEGORY:
        return d["value"] == value
    return True


class FacilityReviewTabMixin(object):
    def _build_facility_review_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        info = QLabel(
            "<h3>Review: roads, railroads, bridges and lifeline facilities</h3>The overtopped "
            "road stretches, inundated railroad stretches, bridges / culverts / low-water "
            "crossings and lifeline facilities of the last Hazard Classification run. For each "
            "one choose '{}', <b>Exclude</b> it (not a public road, abandoned line, not a "
            "lifeline ...), set the <b>AADT</b>, <b>trains per day</b> or <b>category</b>, or "
            "say whether a bridge is <b>overtopped</b>. Decisions are saved to <i>{}</i> in the "
            "outputs folder and applied on every later run: excluded items are left out of the "
            "classification and the output layers, set values replace the automatic ones. "
            "Click a column heading to sort; double-click a row to zoom to it."
            .format(_CONFIRM, FR.REVIEW_FILE))
        info.setWordWrap(True)
        lay.addWidget(info)
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Show:"))
        self.facrev_kind_combo = QComboBox()
        for k in FR.KINDS:
            self.facrev_kind_combo.addItem(FR.KIND_LABELS[k], k)
        self.facrev_kind_combo.currentIndexChanged.connect(self._facrev_view_changed)
        opts.addWidget(self.facrev_kind_combo)
        opts.addWidget(QLabel("Scenario:"))
        self.facrev_scenario_combo = QComboBox()
        self.facrev_scenario_combo.setToolTip(
            "The breach scenario whose items are listed (the governing one first). A decision "
            "belongs to the place, so it applies in every scenario where the item is found.")
        self.facrev_scenario_combo.currentIndexChanged.connect(self._facrev_view_changed)
        opts.addWidget(self.facrev_scenario_combo)
        self.facrev_show_all_check = QCheckBox("List every item (not only flooded / flagged)")
        self.facrev_show_all_check.setChecked(False)
        self.facrev_show_all_check.setToolTip(
            "Unticked: roads that count (ND High / Medium) or are flagged (no AADT, bridge, "
            "private ...), every railroad stretch, and the flooded bridges / crossings and "
            "lifeline facilities - plus every item with a saved decision. Ticked: all of them.")
        self.facrev_show_all_check.toggled.connect(self._facrev_view_changed)
        opts.addWidget(self.facrev_show_all_check)
        opts.addStretch()
        lay.addLayout(opts)
        self.facrev_table = QTableWidget()
        try:
            self.facrev_table.cellDoubleClicked.connect(self._facrev_zoom)
        except Exception:
            pass
        self._facrev_sort = {}               # kind -> (column, descending)
        try:
            hh = self.facrev_table.horizontalHeader()
            hh.setSectionsClickable(True)
            hh.setSortIndicatorShown(False)
            hh.sectionClicked.connect(self._facrev_sort_by)
        except Exception as e:
            log_message("Road / facility review: sorting by column not available ({})".format(e))
        lay.addWidget(self.facrev_table)
        row = QHBoxLayout()
        self.facrev_zoom_btn = QPushButton("Zoom to selected")
        self.facrev_zoom_btn.clicked.connect(self._facrev_zoom)
        self.facrev_save_btn = QPushButton("Save decisions")
        self.facrev_save_btn.clicked.connect(self._facrev_save)
        self.facrev_rerun_btn = QPushButton("Save and re-run classification")
        self.facrev_rerun_btn.clicked.connect(self._facrev_save_and_rerun)
        for b in (self.facrev_zoom_btn, self.facrev_save_btn, self.facrev_rerun_btn):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        self.facrev_status = QLabel("Run Hazard Classification first")
        self.facrev_status.setWordWrap(True)
        lay.addWidget(self.facrev_status)
        self._facrev_rows = []
        self._facrev_edits = {}
        self._facrev_run = None
        self._facrev_filling = False
        self._facrev_set_columns(FR.KIND_ROAD)
        return tab

    # ------------------------------------------------------------ helpers --
    def _facrev_kind(self):
        return self.facrev_kind_combo.currentData() or FR.KIND_ROAD

    def _facrev_file(self):
        res = getattr(self, "_last_run", None) or {}
        path = res.get("facility_review_file")
        if path:
            return path
        if res.get("review_file"):
            return FR.review_path(os.path.dirname(res["review_file"]))
        out = getattr(self, "config_output_folder", "")
        return FR.review_path(out) if out else None

    @staticmethod
    def _facrev_breaches(res_all):
        """Breach scenario names of a run, the governing one first."""
        from .scenarios import ROLE_BREACH
        results = res_all.get("results") or {}
        names = [n for n, r in results.items()
                 if getattr(r.get("scenario"), "role", ROLE_BREACH) == ROLE_BREACH]
        gov = res_all.get("governing")
        if gov in names:
            names.remove(gov)
            names.insert(0, gov)
        return names

    def _facrev_set_columns(self, kind):
        heads = columns(kind) + [h if h is not None else VALUE_HEADER[kind] for h in TRAILING]
        self.facrev_table.setColumnCount(len(heads))
        self.facrev_table.setHorizontalHeaderLabels(heads)
        return heads

    def _facrev_widgets(self, i):
        n = len(columns(self._facrev_rows[i]["kind"]))
        t = self.facrev_table
        return t.cellWidget(i, n + 1), t.cellWidget(i, n + 2), t.cellWidget(i, n + 3)

    def _facrev_values(self, i):
        """(decision, value, note) the row's widgets show now."""
        kind = self._facrev_rows[i]["kind"]
        dec_w, val_w, note_w = self._facrev_widgets(i)
        dec = dec_w.currentData() if dec_w is not None else None
        value = None
        if val_w is not None:
            if kind == FR.KIND_LIFELINE:
                value = val_w.currentData()
            else:
                v = _f(val_w.value())
                value = None if v is None or v < 0 else (
                    float(int(round(v))) if kind == FR.KIND_ROAD else round(v, 1))
        note = note_w.text().strip() if note_w is not None else ""
        return (dec, value, note)

    def _facrev_capture(self):
        """Keep the widget values that differ from the saved decision (they
        survive a rebuild of the list)."""
        for i, e in enumerate(getattr(self, "_facrev_rows", []) or []):
            try:
                vals = self._facrev_values(i)
            except Exception:
                continue
            k = (e["scenario"], e["kind"], e["key"])
            if vals == saved_triple(e["kind"], e["saved"]):
                self._facrev_edits.pop(k, None)
            else:
                self._facrev_edits[k] = vals

    def _facrev_clear(self):
        """Nothing from an earlier run / project may be edited."""
        self._facrev_rows = []
        self._facrev_edits = {}
        self._facrev_run = None
        self.facrev_table.setRowCount(0)
        self.facrev_status.setText("Run Hazard Classification first")

    def _facrev_fill_scenarios(self, res_all):
        self._facrev_filling = True
        try:
            self.facrev_scenario_combo.clear()
            for n in self._facrev_breaches(res_all):
                self.facrev_scenario_combo.addItem(n, n)
            self.facrev_scenario_combo.setEnabled(self.facrev_scenario_combo.count() > 1)
        finally:
            self._facrev_filling = False

    # ----------------------------------------------------------- the list --
    def _facrev_view_changed(self, *args):
        if getattr(self, "_facrev_filling", False):
            return
        self._populate_facility_review(True)

    def _populate_facility_review(self, *args):
        if getattr(self, "_busy", False) and getattr(self, "_task", None) is not None:
            return                    # a background run is changing the results
        res_all = getattr(self, "_last_run", None)
        if not res_all or not res_all.get("results"):
            self._facrev_clear()
            return
        if res_all is not self._facrev_run:
            self._facrev_rows, self._facrev_edits = [], {}     # another run: other items
            self._facrev_run = res_all
            self._facrev_fill_scenarios(res_all)
        else:
            self._facrev_capture()
        kind = self._facrev_kind()
        scen = self.facrev_scenario_combo.currentData()
        res = (res_all.get("results") or {}).get(scen)
        self._facrev_set_columns(kind)
        if res is None:
            self._facrev_rows = []
            self.facrev_table.setRowCount(0)
            self.facrev_status.setText("No breach scenario in the last run")
            return
        crs = res_all.get("crs")
        ctx = QgsProject.instance().transformContext()
        store = FR.FacilityReviewStore(self._facrev_file())
        items = FR.automatic_items(res, kind)
        matched = {}
        if store.error is None and crs is not None:
            matched = store.match(kind, [r for _k, r in items], crs, ctx)
        run_sum = res.get(FR.RESULT_KEY) or {}
        applied = run_sum.get("applied") or {}
        show_all = self.facrev_show_all_check.isChecked()
        entries, hidden = [], 0
        for i, (key, rec) in enumerate(items):
            saved = matched.get(i)
            edit = self._facrev_edits.get((scen, kind, key))
            if not show_all and saved is None and edit is None and \
                    (kind, key) not in applied and not FR.listed_by_default(res, kind, rec):
                hidden += 1
                continue
            ap = applied.get((kind, key))
            entries.append({"scenario": scen, "kind": kind, "key": key, "rec": rec,
                            "saved": saved, "applied": ap,
                            "cells": info_cells(res, kind, key, rec,
                                                eff=rec if ap is None else ap.get("rec"))})
        for e in entries:
            e["reviewed"] = self._facrev_reviewed_text(e)
        entries = self._facrev_sorted(entries, kind)
        self._facrev_rows = entries
        t = self.facrev_table
        t.setRowCount(0)
        t.setRowCount(len(entries))
        n = len(columns(kind))
        for i, e in enumerate(entries):
            for c, (text, _s) in enumerate(e["cells"]):
                t.setItem(i, c, QTableWidgetItem(text))
            t.setItem(i, n, QTableWidgetItem(e["reviewed"]))
            dec, value, note = self._facrev_edits.get((scen, kind, e["key"]),
                                                      saved_triple(e["kind"], e["saved"]))
            dec_w = QComboBox()
            for label, code in DECISION_CHOICES[kind]:
                dec_w.addItem(label, code)
            if e["saved"] is not None or dec == _CLEAR:
                dec_w.addItem(_CLEAR, _CLEAR)
            k = dec_w.findData(dec) if dec is not None else 0
            dec_w.setCurrentIndex(k if k is not None and k >= 0 else 0)
            val_w = self._facrev_value_widget(kind, value)
            note_w = QLineEdit()
            note_w.setText(note or "")
            t.setCellWidget(i, n + 1, dec_w)
            if val_w is None:
                t.setItem(i, n + 2, QTableWidgetItem(""))
            else:
                t.setCellWidget(i, n + 2, val_w)
            t.setCellWidget(i, n + 3, note_w)
        try:
            t.resizeColumnsToContents()
        except Exception:
            pass
        self._facrev_show_sort()
        n_saved = sum(1 for e in entries if e["saved"] is not None)
        pending = sum(1 for k in self._facrev_edits if k[0] == scen and k[1] == kind)
        parts = ["{}{}: {} listed".format(FR.KIND_LABELS[kind],
                                          " ('{}')".format(scen) if
                                          self.facrev_scenario_combo.count() > 1 else "",
                                          len(entries))]
        if hidden:
            parts[0] += " ({} more - tick 'List every item')".format(hidden)
        parts.append("{} with a saved decision".format(n_saved))
        if pending:
            parts.append("{} unsaved change(s)".format(pending))
        if store.error is not None:
            parts.append("{} could not be read ({})".format(
                os.path.basename(store.path or FR.REVIEW_FILE), store.error))
        else:
            parts.append("{} decision(s) in {}".format(
                len(store), os.path.basename(store.path or FR.REVIEW_FILE)))
        if run_sum.get("unmatched"):
            parts.append("{} saved decision(s) matched nothing in this scenario of the last "
                         "run".format(len(run_sum["unmatched"])))
        self.facrev_status.setText("; ".join(parts))
        return len(entries)

    @staticmethod
    def _facrev_value_widget(kind, value):
        if kind == FR.KIND_ROAD:
            w = QSpinBox()
            w.setRange(-1, 1000000)
            w.setSpecialValueText("(auto)")
            w.setValue(-1 if value is None else int(round(float(value))))
            return w
        if kind == FR.KIND_RAIL:
            w = QDoubleSpinBox()
            w.setDecimals(1)
            w.setRange(-1.0, 1000.0)
            w.setSpecialValueText("(auto)")
            w.setValue(-1.0 if value is None else float(value))
            return w
        if kind == FR.KIND_LIFELINE:
            w = QComboBox()
            w.addItem("(auto)", None)
            for c in FR.CATEGORY_CHOICES:
                w.addItem(c, c)
            k = w.findData(value) if value is not None else 0
            w.setCurrentIndex(k if k is not None and k >= 0 else 0)
            return w
        return None

    @staticmethod
    def _facrev_reviewed_text(e):
        """What the last run applied and what the file says now."""
        ap, saved = e.get("applied"), e.get("saved")
        text = ap["text"] if ap else ""
        if saved is None:
            return text + (" - removed, applies on the next run" if ap else "")
        d = FR.decision_of(saved)
        if ap and ap.get("decision") == d["decision"] and \
                (ap.get("note") or "") == (d["note"] or "") and \
                (ap.get("value") == d["value"] or (
                    _f(ap.get("value")) is not None and _f(d["value"]) is not None and
                    abs(_f(ap["value"]) - _f(d["value"])) < 1e-9)):
            return text
        now = "saved: {}{}".format(d["decision"] or d["raw_decision"] or "?",
                                   " {}".format(d["value_text"]) if d["value_text"] else "")
        return (text + " - " if text else "") + now + ", applies on the next run"

    # ------------------------------------------------------------ sorting --
    def _facrev_sort_value(self, e, col, n_info):
        if col < n_info:
            return e["cells"][col][1]
        if col == n_info:
            return _txt(e.get("reviewed"))
        vals = self._facrev_edits.get((e["scenario"], e["kind"], e["key"]),
                                      saved_triple(e["kind"], e["saved"]))
        dec, value, note = vals
        if col == n_info + 1:
            label = next((lab for lab, code in DECISION_CHOICES[e["kind"]] if code == dec),
                         _CLEAR if dec == _CLEAR else None)
            return None if dec is None else _txt(label)
        if col == n_info + 2:
            return _txt(value) if e["kind"] == FR.KIND_LIFELINE else _f(value)
        return _txt(note)

    def _facrev_sorted(self, entries, kind):
        sort = (getattr(self, "_facrev_sort", None) or {}).get(kind)
        if not sort:
            return entries
        col, desc = sort
        n = len(columns(kind))
        try:
            keyed = [(self._facrev_sort_value(e, col, n), e) for e in entries]
            full = [ke for ke in keyed if ke[0] is not None]
            full.sort(key=lambda ke: ke[0], reverse=bool(desc))
        except Exception as ex:                   # never lose the list over a sort
            log_message("Road / facility review list not sorted: {}".format(ex))
            return entries
        return [e for _k, e in full] + [e for k, e in keyed if k is None]

    def _facrev_show_sort(self):
        try:
            hh = self.facrev_table.horizontalHeader()
            sort = (getattr(self, "_facrev_sort", None) or {}).get(self._facrev_kind())
            if sort is None:
                hh.setSortIndicatorShown(False)
            else:
                hh.setSortIndicator(sort[0], SORT_DESC if sort[1] else SORT_ASC)
                hh.setSortIndicatorShown(True)
        except Exception:
            pass

    def _facrev_sort_by(self, col, *args):
        """A heading was clicked: sort by it ascending; again - descending."""
        try:
            col = int(col)
        except (TypeError, ValueError):
            return
        kind = self._facrev_kind()
        if not 0 <= col < len(columns(kind)) + len(TRAILING):
            return
        if getattr(self, "_busy", False) and getattr(self, "_task", None) is not None:
            self._facrev_show_sort()
            return
        cur = self._facrev_sort.get(kind)
        self._facrev_sort[kind] = (col, (not cur[1]) if cur and cur[0] == col else False)
        self._facrev_show_sort()
        if getattr(self, "_last_run", None):
            self._populate_facility_review(True)

    # -------------------------------------------------------------- saving --
    def _facrev_save(self, *args):
        """Saves every changed decision (all kinds / scenarios); returns
        their number, or None when nothing could be saved."""
        if getattr(self, "_busy", False):
            return None
        res_all = getattr(self, "_last_run", None)
        if not res_all or not res_all.get("results"):
            QMessageBox.warning(self, "Review", "Run Hazard Classification first.")
            return None
        self._facrev_capture()
        path = self._facrev_file()
        if not path:
            QMessageBox.warning(self, "Review", "Pick the dam folder first.")
            return None
        store = FR.FacilityReviewStore(path)
        if store.error is not None:
            QMessageBox.warning(self, "Review", "{} could not be read ({}), so it was not "
                                "overwritten. Fix or rename it and save again.".format(
                                    path, store.error))
            return None
        crs = res_all.get("crs")
        ctx = QgsProject.instance().transformContext()
        groups = {}
        for (scen, kind, key), vals in self._facrev_edits.items():
            groups.setdefault((scen, kind), []).append((key, vals))
        saved = removed = kept = 0
        problems, left = [], {}
        for (scen, kind), edits in groups.items():
            res = (res_all.get("results") or {}).get(scen)
            if res is None:
                continue
            items = FR.automatic_items(res, kind)
            index = {k: (i, r) for i, (k, r) in enumerate(items)}
            matched = store.match(kind, [r for _k, r in items], crs, ctx)
            for key, (dec, value, note) in edits:
                if key not in index:
                    continue
                i, rec = index[key]
                old = matched.get(i)
                if dec is None and old is not None:
                    # "(no change)" with a new note / value: the saved decision stays
                    od = FR.decision_of(old)
                    if od["decision"] in FR.ALLOWED.get(kind, ()) and \
                            od["decision"] != FR.DEC_NOTE and \
                            (value is None or od["decision"] == FR.VALUE_DECISION.get(kind)):
                        dec = od["decision"]
                        value = od["value"] if value is None else value
                d, v = to_decision(kind, dec, value, note)
                if d is None:
                    continue
                if d.startswith("!"):
                    problems.append("{} {}: {}".format(FR.KIND_LABELS[kind], key, d[1:]))
                    left[(scen, kind, key)] = (dec, value, note)
                    continue
                if d == _CLEAR:
                    if old is not None and store.remove(old):
                        removed += 1
                    continue
                if _same(old, d, v, note):
                    kept += 1                    # keeps the original reviewer and date
                    continue
                if store.set_decision(kind, rec, crs, ctx, d, value=v, note=note, replace=old,
                                      item_id=key) is None:
                    problems.append("{} {}: no location".format(FR.KIND_LABELS[kind], key))
                    continue
                saved += 1
        try:
            store.save()
        except RuntimeError as e:
            QMessageBox.warning(self, "Review", str(e))
            return None
        self._facrev_edits = left
        msg = "{} road / facility decision(s) saved{}{} to {}".format(
            saved, ", {} removed".format(removed) if removed else "",
            ", {} unchanged".format(kept) if kept else "", store.path)
        if problems:
            msg += " - not saved: " + "; ".join(problems)
            QMessageBox.warning(self, "Review", "Not saved:\n" + "\n".join(problems))
        self._log(">>> Review (roads & facilities): " + msg)
        self._facrev_rows_rebuild_after_save()
        self.facrev_status.setText(msg)
        return saved + removed

    def _facrev_rows_rebuild_after_save(self):
        self._facrev_rows = []                   # the widgets now show the file
        self._populate_facility_review(True)

    def _facrev_save_and_rerun(self, *args):
        if self._facrev_save() is None:
            return
        if hasattr(self, "_on_run_classification"):
            self._on_run_classification()

    # ---------------------------------------------------------------- zoom --
    def _facrev_aerial(self):
        """The Review tab's 'Aerial imagery when zooming' box applies here too."""
        box = getattr(self, "review_aerial_check", None)
        if box is None or not box.isChecked():
            return
        try:
            from . import basemap
            state, text = basemap.ensure_aerial()
            if state == "added":
                self._log(">>> Review: aerial imagery added at the bottom of the layer tree "
                          "({})".format(text))
            elif state == "failed":
                self._log(">>> Review: {}".format(text))
        except Exception as e:
            log_message("Aerial imagery: {}".format(e))

    def _facrev_zoom(self, *args):
        if getattr(self, "_busy", False) and getattr(self, "_task", None) is not None:
            return
        r = self.facrev_table.currentRow()
        if args and isinstance(args[0], int) and not isinstance(args[0], bool):
            r = args[0]                       # double-clicked row (clicked() sends a bool)
        rows = getattr(self, "_facrev_rows", []) or []
        if r is None or r < 0 or r >= len(rows):
            return
        rec = rows[r]["rec"]
        geom = rec.get("geom")
        if geom is None:
            return
        try:
            from qgis.utils import iface
            canvas = iface.mapCanvas()
        except Exception:
            canvas = None
        if canvas is None:
            QMessageBox.information(self, "Review", "The map canvas is not available.")
            return
        self._facrev_aerial()
        try:
            crs = self._last_run["crs"]
            dest = canvas.mapSettings().destinationCrs()
            xf = QgsCoordinateTransform(crs, dest, QgsProject.instance().transformContext())
            bb = QgsRectangle(geom.boundingBox())
            # a stretch can be long, a facility a point: a quarter of its size, >= 150 ft
            pad = max(0.25 * max(bb.width(), bb.height()),
                      buffer_distance_in_layer_units(150.0, crs))
            bb = QgsRectangle(bb.xMinimum() - pad, bb.yMinimum() - pad, bb.xMaximum() + pad,
                              bb.yMaximum() + pad)
            canvas.setExtent(bb if xf.isShortCircuited() else xf.transformBoundingBox(bb))
            canvas.refresh()
            try:
                canvas.flashGeometries([geom], crs)
            except Exception:
                pass
        except Exception as e:
            log_message("Zoom failed: {}".format(e))
