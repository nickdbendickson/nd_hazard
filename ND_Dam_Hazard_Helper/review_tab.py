# -*- coding: utf-8 -*-
"""'Review' tab: step through the structures that need the engineer's
decision (unverified habitability, judgment zone, borderline, moved address
points, lifelines, cut-off access), zoom to them, record decisions. The
decisions are saved to outputs/Structure_Review_Decisions.csv and applied
automatically on every later classification run (review.py).

Structures that already have a saved decision are hidden after the next run
(tick "Show structures already reviewed" to see and change them).

v2.9: clicking a column heading sorts the list by that column (again:
reversed). The table's own sorting (setSortingEnabled) is NOT used: the
decision widgets live in the cells and row i of the table is
self._review_rows[i], so the rows are sorted here and the table is rebuilt
(unsaved edits kept, as when an option is ticked). The list also shows the
maximum velocity and the site address, and "Open in web map" opens the
selected structure's location in the browser (webmap.py)."""

import math
import os

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QWidget, QTableWidget, QTableWidgetItem,
    QComboBox, QSpinBox, QLineEdit, QMessageBox, QCheckBox, QFileDialog, QInputDialog,
)
from qgis.core import QgsProject, QgsCoordinateTransform, QgsRectangle

from . import webmap
from .compat import MB_YES, MB_NO
from .constants import ZONE_LABELS
from .helpers import log_message, buffer_distance_in_layer_units
from .review import ReviewStore, needs_review, review_path, CONFIRMED
from .structures import (
    CAT_DWELLING, CAT_OCCUPIED, CAT_UNVERIFIED, CAT_NOT_HABITABLE, TYPE_HOUSE, TYPE_MOBILE,
    TYPE_HABITABLE, address_of,
)

# columns
# v2.5.1: the FEMA occupancy is shown for reference only; habitability is
# its own column (and decision)
# v2.9: the site address after "Why review", the maximum velocity after the depth
(C_ID, C_WHY, C_ADDR, C_STATUS, C_CAT, C_HAB, C_TYPE, C_ZONE, C_DEPTH, C_VEL, C_PERS,
 C_SET_CAT, C_SET_TYPE, C_SET_PERS, C_NOTE) = range(15)
REVIEW_COLS = ["ID", "Why review", "Address", "Reviewed", "FEMA category", "Habitable (now)",
               "ACER type (now)", "ACER zone", "Depth ft", "Vel ft/s", "Persons (now)",
               "Set habitable", "Set ACER type", "Set persons", "Note"]
# 2.30.0: the "what would change it" table (whatif.py)
WHATIF_COLS = ["Item", "Now", "What would change it", "Resulting class", "Direction"]


def _qt_sort_order(name):
    """Qt.SortOrder member: the scoped spelling first (PyQt6 needs it, recent
    PyQt5 has it too), the legacy Qt5 spelling second - as compat.py resolves
    enums (compat.py has no sort-order shim)."""
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
# ACER zone sorts by severity (HIGH first when ascending); DRY counts as empty
_ZONE_RANK = {1: 0, 2: 1, 3: 2, -9999: 3}
_KEEP = "(no change)"
_CONFIRM = "Automatic result is correct"
_CLEAR = "(remove saved decision)"
# (label, value saved in the decisions file)
# (the saved values are unchanged, so earlier decision files still apply)
CATEGORY_CHOICES = [(_KEEP, None), (_CONFIRM, CONFIRMED),
                    ("Habitable - dwelling", CAT_DWELLING),
                    ("Habitable - occupied non-residential", CAT_OCCUPIED),
                    ("Unverified", CAT_UNVERIFIED), ("Not habitable", CAT_NOT_HABITABLE),
                    (_CLEAR, _CLEAR)]
CATEGORIES = [label for label, _v in CATEGORY_CHOICES]
TYPES = [_KEEP, TYPE_HABITABLE, TYPE_HOUSE, TYPE_MOBILE]


def _unchanged(rv, cat, typ, pers, note):
    """True when the table shows exactly the decision already saved."""
    if not rv:
        return False
    return (rv.get("category") == cat and rv.get("acer_type") == typ and
            rv.get("persons") == pers and (rv.get("note") or "") == (note or ""))


# ------------------------------------------------------------ v2.9: sorting --
def _number(v):
    """Finite float, or None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _num_text(v, fmt="{:.1f}"):
    f = _number(v)
    return "" if f is None else fmt.format(f)


def _text_key(s):
    s = (s or "").strip()
    return s.casefold() if s else None


def _sort_key(col, rec, texts, widgets):
    """Sort value of a row in column `col`, None when the cell is empty
    (empty cells are listed last in both directions). Numbers sort as
    numbers, text case-insensitively, the decision columns by what their
    widget shows now ("(no change)" / "(auto)" / no note = empty)."""
    if col == C_ID:
        n = _number(rec.get("id"))
        if n is not None:
            return (0, n, "")
        t = _text_key(texts.get(C_ID))
        return None if t is None else (1, 0.0, t)
    if col == C_DEPTH:
        return _number(rec.get("max_depth")) or None       # 0 ft is shown blank (dry)
    if col == C_VEL:
        return _number(rec.get("max_vel"))
    if col == C_PERS:
        return _number(rec.get("persons") or 0)
    if col == C_ZONE:
        return _ZONE_RANK.get(rec.get("zone"))
    w = widgets.get(col)
    if col in (C_SET_CAT, C_SET_TYPE):
        if w is None or w.currentData() is None:
            return None
        return _text_key(w.currentText())
    if col == C_SET_PERS:
        v = None if w is None else _number(w.value())
        return None if v is None or v < 0 else v
    if col == C_NOTE:
        return None if w is None else _text_key(w.text())
    return _text_key(texts.get(col))


def sort_entries(entries, sort):
    """entries [(rec, why, texts, widgets)] in the order `sort` = (column,
    descending) asks for; None keeps the order given (needs_review: most
    important first). Ties keep that order too."""
    if not sort:
        return list(entries)
    col, desc = sort
    try:
        keyed = [(_sort_key(col, e[0], e[2], e[3]), e) for e in entries]
        full = [ke for ke in keyed if ke[0] is not None]
        full.sort(key=lambda ke: ke[0], reverse=bool(desc))   # stable, also reversed
    except Exception as e:                    # never lose the list over a sort
        log_message("Review list not sorted: {}".format(e))
        return list(entries)
    return [e for _k, e in full] + [e for k, e in keyed if k is None]


class ReviewTabMixin(object):
    def _build_review_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        # 2.30.0: the plain-language result card first - the class, the one reason for it,
        # what would change it and the flags (resultcard.py; the same card heads the report).
        self.review_card = QLabel("No classification run yet - the result card appears here "
                                  "after a run.")
        self.review_card.setWordWrap(True)
        try:                        # Qt6 scopes the flag, Qt5 has it on Qt itself
            flag = getattr(getattr(Qt, "TextInteractionFlag", Qt), "TextSelectableByMouse")
            self.review_card.setTextInteractionFlags(flag)
        except Exception:                                        # noqa - selection is a nicety
            pass
        self.review_card.setStyleSheet(
            "padding: 10px; background-color: #f7f7f7; border: 1px solid #d0d0d0;")
        lay.addWidget(self.review_card)
        # 2.30.0: "what would change it" - the items nearest a class boundary (whatif.py)
        self.whatif_label = QLabel("<b>What would change the class</b> - the items nearest a "
                                   "boundary, closest first. A row that would LOWER the class "
                                   "is the engineer's decision: the plugin never applies it.")
        self.whatif_label.setWordWrap(True)
        lay.addWidget(self.whatif_label)
        self.whatif_table = QTableWidget()
        self.whatif_table.setColumnCount(len(WHATIF_COLS))
        self.whatif_table.setHorizontalHeaderLabels(WHATIF_COLS)
        self.whatif_table.setRowCount(0)
        self.whatif_table.setMaximumHeight(190)
        self.whatif_table.setToolTip("Double-click a row to zoom to the structure, road or "
                                     "railroad stretch behind it")
        try:
            self.whatif_table.cellDoubleClicked.connect(self._whatif_zoom)
        except Exception:
            pass
        lay.addWidget(self.whatif_table)
        self._whatif_rows = []
        info = QLabel(
            "<h3>Engineer review</h3>Structures that need a decision after the last Hazard "
            "Classification run. <b>Set habitable</b> decides habitability (structures with "
            "an ND site address are habitable automatically), <b>Set ACER type</b> decides "
            "the curve (Habitable and House on foundation: TM 11 Fig. 2; Mobile home: Fig. 3), "
            "<b>Set persons</b> overrides the occupancy. The FEMA category is shown for "
            "reference only. Choose '{}' when "
            "the plugin got it right. Decisions are saved to <i>{}</i> in the outputs folder, "
            "applied on every later run, and the structures are then no longer listed "
            "(tick the box below to see them again). Double-click a row to zoom to it; "
            "click a column heading to sort the list by it (click again to reverse)."
            .format(_CONFIRM, os.path.basename(review_path("x"))))
        info.setWordWrap(True)
        lay.addWidget(info)
        opts = QHBoxLayout()
        self.review_show_done = QCheckBox("Show structures already reviewed")
        self.review_show_done.setChecked(False)
        self.review_show_all = QCheckBox("List every flooded structure (not only flagged)")
        self.review_show_all.setChecked(False)
        for cb in (self.review_show_done, self.review_show_all):
            cb.toggled.connect(self._populate_review)
            opts.addWidget(cb)
        # v2.7: aerial photo under the map when zooming to a structure
        self.review_aerial_check = QCheckBox("Aerial imagery when zooming")
        self.review_aerial_check.setChecked(True)
        self.review_aerial_check.setToolTip(
            "Adds an aerial photo (USGS National Map by default) at the bottom of the layer "
            "tree the first time you zoom to a structure, so you can see what it is. Needs "
            "an internet connection; untick to switch it off. The address can be changed "
            "in the data-source file.")
        self.review_aerial_check.toggled.connect(self._aerial_toggled)
        opts.addWidget(self.review_aerial_check)
        opts.addStretch()
        lay.addLayout(opts)
        self.review_table = QTableWidget()
        self.review_table.setColumnCount(len(REVIEW_COLS))
        self.review_table.setHorizontalHeaderLabels(REVIEW_COLS)
        try:
            self.review_table.cellDoubleClicked.connect(self._review_zoom)
        except Exception:
            pass
        # v2.9: sorting by a column heading (done here, not by the table - see the docstring)
        self._review_sort = None              # (column, descending) once a heading is clicked
        try:
            hh = self.review_table.horizontalHeader()
            hh.setSectionsClickable(True)
            hh.setSortIndicatorShown(False)
            hh.sectionClicked.connect(self._review_sort_by)
        except Exception as e:
            log_message("Review list: sorting by column not available ({})".format(e))
        lay.addWidget(self.review_table)
        row = QHBoxLayout()
        self.review_zoom_btn = QPushButton("Zoom to selected")
        self.review_zoom_btn.clicked.connect(self._review_zoom)
        # v2.9: a street-level look at the selected structure in the web browser
        self.review_webmap_btn = QPushButton("Open in web map")
        self.review_webmap_btn.setToolTip(
            "Opens the selected structure's location in your web browser (Google Maps by "
            "default - drag the Street View figure onto the map for a street-level look). "
            "Only the latitude / longitude is sent. Needs an internet connection; the web "
            "address can be changed in the data-source file (WEBMAP_URL_TEMPLATE).")
        self.review_webmap_btn.clicked.connect(self._review_open_webmap)
        self.review_save_btn = QPushButton("Save decisions")
        self.review_save_btn.clicked.connect(self._review_save)
        self.review_rerun_btn = QPushButton("Save and re-run classification")
        self.review_rerun_btn.clicked.connect(self._review_save_and_rerun)
        # v2.8: two engineers review the same dam separately and merge their decisions
        self.review_export_btn = QPushButton("Export decisions...")
        self.review_export_btn.setToolTip(
            "Writes the saved decisions as a portable CSV (with a small header file) to send "
            "to another engineer. Save the decisions first.")
        self.review_export_btn.clicked.connect(self._review_export)
        self.review_merge_btn = QPushButton("Merge from other engineer...")
        self.review_merge_btn.setToolTip(
            "Compares another engineer's exported decisions with yours, lists the differences "
            "and - after you choose how to settle conflicts - merges them (a backup copy of "
            "your decisions is made first). Re-run the classification to apply them.")
        self.review_merge_btn.clicked.connect(self._review_merge)
        # 2.30.0: the field-check sheet for the site visit after a screening High, and the
        # marked-up sheet back into the engineer's review decisions (fieldcheck.py)
        self.field_sheet_btn = QPushButton("Field-check sheet...")
        self.field_sheet_btn.setToolTip(
            "Writes a one-page-per-structure sheet for the site visit (address, what the data "
            "says, depth, velocity, a photo slot and a habitable yes / no box) plus the CSV "
            "to fill in and import again.")
        self.field_sheet_btn.clicked.connect(self._field_sheet_write)
        self.field_import_btn = QPushButton("Import field-check sheet...")
        self.field_import_btn.setToolTip(
            "Reads the marked-up Field_Check_Sheet.csv back in: the answers become engineer "
            "review decisions (the same file the Review tab saves) and apply on the next "
            "classification run. 'Habitable: no' can lower the class, so those rows are "
            "listed before anything is saved.")
        self.field_import_btn.clicked.connect(self._field_sheet_import)
        for b in (self.review_zoom_btn, self.review_webmap_btn, self.review_save_btn,
                  self.review_rerun_btn, self.review_export_btn, self.review_merge_btn,
                  self.field_sheet_btn, self.field_import_btn):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        self.review_status = QLabel("Run Hazard Classification first")
        self.review_status.setWordWrap(True)
        lay.addWidget(self.review_status)
        self._review_rows = []
        return tab

    def _pending_edits(self):
        """{structure id: (category, type, persons, note)} of rows whose
        widgets differ from the saved decision - kept when the list is
        rebuilt (ticking an option must not throw edits away)."""
        out = {}
        t = self.review_table
        for i, (rec, _why) in enumerate(getattr(self, "_review_rows", []) or []):
            try:
                vals = (t.cellWidget(i, C_SET_CAT).currentData(),
                        t.cellWidget(i, C_SET_TYPE).currentData(),
                        t.cellWidget(i, C_SET_PERS).value(),
                        t.cellWidget(i, C_NOTE).text())
            except Exception:
                continue
            out[rec["id"]] = vals
        return out

    def _pending_status(self):
        """{structure id: text} of "Reviewed" cells changed since the list was
        built ("saved - applied on the next run") - kept when it is rebuilt."""
        out = {}
        t = self.review_table
        for i, (rec, _why) in enumerate(getattr(self, "_review_rows", []) or []):
            try:
                text = t.item(i, C_STATUS).text()
            except Exception:
                continue
            if text != (rec.get("reviewed") or ""):
                out[rec["id"]] = text
        return out

    @staticmethod
    def _review_texts(rec, why):
        """{column: text} of the read-only cells of a structure."""
        try:
            addr, city, _zip = address_of(rec)
        except Exception:
            addr = city = ""
        return {C_ID: str(rec["id"]), C_WHY: "; ".join(why),
                C_ADDR: ", ".join(p for p in (addr, city) if p),
                C_STATUS: rec.get("reviewed") or "",
                C_CAT: rec.get("fema_category") or "", C_HAB: rec.get("habitable") or "",
                C_TYPE: rec["acer_type"],
                C_ZONE: ZONE_LABELS.get(rec.get("zone"), "DRY"),
                C_DEPTH: "" if not rec.get("max_depth") else _num_text(rec["max_depth"]),
                C_VEL: _num_text(rec.get("max_vel")),
                C_PERS: str(rec.get("persons") or 0)}

    @staticmethod
    def _review_widgets(rv, edit):
        """{column: widget} of the decision cells: pre-filled from the saved
        decision `rv`, then from the unsaved `edit` (category, type, persons,
        note) when there is one."""
        cat = QComboBox()
        for label, value in CATEGORY_CHOICES:
            if value == _CLEAR and not rv:
                continue                  # nothing saved to remove
            cat.addItem(label, value)
        saved_cat = rv.get("category")
        for k, (_label, value) in enumerate(CATEGORY_CHOICES):
            if value is not None and value == saved_cat:
                cat.setCurrentIndex(k)
        typ = QComboBox()
        for x in TYPES:
            typ.addItem(x, None if x == _KEEP else x)
        if rv.get("acer_type") in TYPES:
            typ.setCurrentIndex(TYPES.index(rv["acer_type"]))
        pers = QSpinBox()
        pers.setRange(-1, 100000)
        pers.setSpecialValueText("(auto)")
        pers.setValue(-1 if rv.get("persons") is None else int(rv["persons"]))
        note = QLineEdit()
        note.setText(rv.get("note", ""))
        if edit is not None:              # unsaved edits survive a rebuild
            e_cat, e_typ, e_pers, e_note = edit
            for combo, value in ((cat, e_cat), (typ, e_typ)):
                k = combo.findData(value) if value is not None else 0
                if k is not None and k >= 0:
                    combo.setCurrentIndex(k)
            pers.setValue(e_pers)
            note.setText(e_note)
        return {C_SET_CAT: cat, C_SET_TYPE: typ, C_SET_PERS: pers, C_NOTE: note}

    def _populate_review(self, *args):
        """(Re)builds the list. With arguments (an option ticked, a column
        heading clicked) the unsaved edits in the table are kept; without (a
        new run, a merge) the rows are pre-filled from the decisions file.
        The chosen sort order applies in both cases."""
        if getattr(self, "_busy", False) and getattr(self, "_task", None) is not None:
            return                    # a background run is changing the structures
        res = getattr(self, "_last_run", None)
        if not res:
            return
        edits = self._pending_edits() if args else {}
        status = self._pending_status() if args else {}
        store = ReviewStore(res["review_file"])
        show_done = self.review_show_done.isChecked()
        rows = needs_review(res["structs"], include_all_wet=self.review_show_all.isChecked())
        n_done = sum(1 for rec, _w in rows if rec.get("review"))
        if not show_done:
            rows = [(rec, why) for rec, why in rows if not rec.get("review")]
        # pre-fill from the file as it is now (it may have been saved since the run)
        saved = store.decisions_for([r for r, _w in rows], res["crs"],
                                    QgsProject.instance().transformContext())
        entries = []
        for rec, why in rows:
            texts = self._review_texts(rec, why)
            if rec["id"] in status:
                texts[C_STATUS] = status[rec["id"]]
            widgets = self._review_widgets(saved.get(rec["id"]) or {}, edits.get(rec["id"]))
            entries.append((rec, why, texts, widgets))
        # v2.9: sorted on the widgets' values before they go into the table
        sort = getattr(self, "_review_sort", None)
        entries = sort_entries(entries, sort)
        self._review_rows = [(rec, why) for rec, why, _t, _w in entries]
        t = self.review_table
        t.setRowCount(len(entries))
        for i, (_rec, _why, texts, widgets) in enumerate(entries):
            for c, v in texts.items():
                t.setItem(i, c, QTableWidgetItem(v))
            for c, w in widgets.items():
                t.setCellWidget(i, c, w)
        try:
            t.resizeColumnsToContents()
        except Exception:
            pass
        self._review_show_sort()
        hidden = "" if show_done or not n_done else \
            " ({} already reviewed - hidden)".format(n_done)
        order = "" if not sort else ", sorted by {} ({})".format(
            REVIEW_COLS[sort[0]], "descending" if sort[1] else "ascending")
        self.review_status.setText(
            "{} structure(s) listed{}{}; {} saved decision(s) in {}".format(
                len(entries), hidden, order, len(store), os.path.basename(store.path)))
        self._populate_card()                      # 2.30.0: the card and the boundary list

    # ------------------------------------- 2.30.0: the card and "what would change it" --
    def _populate_card(self):
        """The result card and the what-would-change-it table of the last run. Never raises:
        both are views of the run, and a view must not stop the Review tab."""
        res = getattr(self, "_last_run", None) or {}
        try:
            from . import resultcard as RC
            card = res.get("card")
            if not card:
                from . import whatif as WI       # a result from before 2.30.0: build one now
                gov = (res.get("results") or {}).get(res.get("governing")) or {}
                card = RC.build(res.get("nd") or {}, res.get("tm11") or {},
                                roads=gov.get("roads") or [], rails=gov.get("rails") or [],
                                access=res.get("access"), margin=res.get("margin"),
                                dam_name=res.get("dam_name") or "",
                                governing_scenario=res.get("governing") or "")
                _ = WI
            self.review_card.setText(RC.html(card))
        except Exception as e:                                   # noqa - never stop the tab
            log_message("Result card not shown: {}".format(e))
            self.review_card.setText("The result card could not be built ({}).".format(e))
        rows = []
        try:
            from . import whatif as WI
            rows = res.get("whatif") or []
            if not rows and res.get("structs"):
                gov = (res.get("results") or {}).get(res.get("governing")) or {}
                rows = WI.candidates(res["structs"], gov.get("roads") or [],
                                     gov.get("rails") or [], res.get("nd") or {})
            table = WI.table_rows(rows)
        except Exception as e:                                   # noqa - never stop the tab
            log_message("'What would change it' not shown: {}".format(e))
            rows, table = [], []
        self._whatif_rows = list(rows)
        t = self.whatif_table
        t.setRowCount(len(table))
        for i, row in enumerate(table):
            for c, v in enumerate(row):
                t.setItem(i, c, QTableWidgetItem("" if v is None else str(v)))
        try:
            t.resizeColumnsToContents()
        except Exception:
            pass

    def _zoom_to_geometry(self, geom, pad_ft=150.0):
        """Zoom the canvas to one geometry of the last run (the analysis CRS). Returns True
        when the canvas moved."""
        try:
            from qgis.utils import iface
            canvas = iface.mapCanvas()
        except Exception:                                        # noqa - no canvas (tests)
            canvas = None
        if canvas is None or geom is None:
            return False
        try:
            crs = self._last_run["crs"]
            dest = canvas.mapSettings().destinationCrs()
            xf = QgsCoordinateTransform(crs, dest, QgsProject.instance().transformContext())
            bb = QgsRectangle(geom.boundingBox())
            pad = max(bb.width() * 0.5, bb.height() * 0.5,
                      buffer_distance_in_layer_units(pad_ft, crs))
            bb = QgsRectangle(bb.xMinimum() - pad, bb.yMinimum() - pad, bb.xMaximum() + pad,
                              bb.yMaximum() + pad)
            canvas.setExtent(bb if xf.isShortCircuited() else xf.transformBoundingBox(bb))
            canvas.refresh()
            try:
                canvas.flashGeometries([geom], crs)
            except Exception:                                    # noqa - older QGIS
                pass
            return True
        except Exception as e:                                   # noqa
            log_message("Zoom failed: {}".format(e))
            return False

    def _whatif_zoom(self, row=None, *args):
        """Zoom to the item of a what-would-change-it row (its geometry, when it has one)."""
        try:
            rows = getattr(self, "_whatif_rows", []) or []
            i = self.whatif_table.currentRow() if row is None else int(row)
            if not (0 <= i < len(rows)):
                return
            geom = rows[i].get("geom")
            if geom is None:
                self.review_status.setText("That row has no location to zoom to.")
                return
            self._zoom_to_geometry(geom)
        except Exception as e:                                   # noqa - never raise in a slot
            log_message("Zoom to the boundary item failed: {}".format(e))

    # ------------------------------------------- 2.30.0: the field-check sheet --
    def _field_sheet_folder(self):
        folder = getattr(self, "config_output_folder", "") or getattr(
            self, "config_dam_folder", "") or ""
        return folder

    def _field_sheet_write(self, *args):
        """Write Field_Check_Sheet.md (one page per structure) and .csv next to the outputs."""
        try:
            from . import fieldcheck as FC
            res = getattr(self, "_last_run", None)
            if not res:
                QMessageBox.warning(self, "Field-check sheet",
                                    "Run the Hazard Classification first.")
                return
            folder = self._field_sheet_folder()
            if not folder or not os.path.isdir(folder):
                QMessageBox.warning(self, "Field-check sheet",
                                    "No outputs folder yet - run the classification first.")
                return
            rows = FC.sheet_rows(res["structs"], address_fn=address_of,
                                 lonlat_fn=lambda rec: self._lonlat_of(rec))
            if not rows:
                QMessageBox.information(self, "Field-check sheet",
                                        "No structure needs a site visit: none is in the ACER "
                                        "high or judgment zone, unverified, borderline or cut "
                                        "off.")
                return
            name = self._review_dam_name()
            md = FC.write_sheet(folder, name, rows)
            csv_path = FC.write_csv(folder, rows)
            self._log(">>> Field-check sheet: {} structure(s) - {} and {}".format(
                len(rows), os.path.basename(md), os.path.basename(csv_path)))
            self.review_status.setText(
                "Field-check sheet written for {} structure(s): {} (print it) and {} (fill it "
                "in, then 'Import field-check sheet...')".format(
                    len(rows), os.path.basename(md), os.path.basename(csv_path)))
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        except Exception as e:                                   # noqa - never raise in a slot
            log_message("Field-check sheet not written: {}".format(e))
            QMessageBox.warning(self, "Field-check sheet",
                                "The sheet could not be written: {}".format(e))

    def _lonlat_of(self, rec):
        """(lon, lat) of a structure, for the sheet (None when it cannot be transformed)."""
        from qgis.core import QgsCoordinateReferenceSystem
        from .review import rep_point
        res = getattr(self, "_last_run", None) or {}
        p = rep_point(rec)
        if p is None or res.get("crs") is None:
            return None, None
        try:
            to_wgs = QgsCoordinateTransform(res["crs"],
                                            QgsCoordinateReferenceSystem("EPSG:4326"),
                                            QgsProject.instance().transformContext())
            q = p if to_wgs.isShortCircuited() else to_wgs.transform(p)
            return q.x(), q.y()
        except Exception:                                        # noqa
            return None, None

    def _field_sheet_import(self, *args):
        """Read a marked-up field-check sheet into the engineer's review decisions."""
        try:
            from . import fieldcheck as FC
            res = getattr(self, "_last_run", None)
            if not res:
                QMessageBox.warning(self, "Field-check sheet",
                                    "Run the Hazard Classification first - the sheet's rows "
                                    "are matched to this run's structures.")
                return
            start = self._field_sheet_folder() or ""
            path, _f = QFileDialog.getOpenFileName(self, "Marked-up field-check sheet", start,
                                                   "CSV (*.csv);;All Files (*)")
            if not path:
                return
            rows = FC.read_csv(path)
            got = FC.summary(rows)
            if not rows:
                QMessageBox.information(self, "Field-check sheet",
                                        "No row of {} has been filled in.".format(
                                            os.path.basename(path)))
                return
            store = ReviewStore(res["review_file"])
            store.attach(res["structs"], res["crs"], QgsProject.instance().transformContext())
            text = ("{} filled-in row(s): {} habitable yes, {} habitable NO, {} could not "
                    "tell.\n\n{} of them would LOWER the hazard class (habitable: no). "
                    "The class only changes when you save the decisions and re-run the "
                    "classification.\n\nImport them as engineer review decisions?".format(
                        got["rows"], got["habitable_yes"], got["habitable_no"],
                        got["unverified"], got["lowering"]))
            if QMessageBox.question(self, "Import field-check sheet", text,
                                    MB_YES | MB_NO) != MB_YES:
                return
            done = FC.apply_to_store(store, res["structs"], res["crs"],
                                     QgsProject.instance().transformContext(), rows)
            store.save()
            for sid, what in done["rows"]:
                self._log(">>> Field check: structure {} - {}".format(sid, what))
            if done["unmatched"]:
                self._log(">>> Field check: {} row(s) matched no structure of this run: "
                          "{}".format(len(done["unmatched"]),
                                      ", ".join(str(x) for x in done["unmatched"][:8])))
            self._populate_review()
            self.review_status.setText(
                "Field check imported: {} decision(s) saved to {} ({} lower the class); "
                "{} row(s) matched nothing. Re-run the classification to apply them.".format(
                    done["applied"], os.path.basename(store.path), done["lowering"],
                    len(done["unmatched"])))
        except Exception as e:                                   # noqa - never raise in a slot
            log_message("Field-check sheet not imported: {}".format(e))
            QMessageBox.warning(self, "Field-check sheet",
                                "The sheet could not be imported: {}".format(e))

    # -------------------------------------------------- v2.9: column sorting --
    def _review_show_sort(self):
        """The heading's sort arrow shows the chosen order (none by default)."""
        try:
            hh = self.review_table.horizontalHeader()
            sort = getattr(self, "_review_sort", None)
            if sort is None:
                hh.setSortIndicatorShown(False)
            else:
                hh.setSortIndicator(sort[0], SORT_DESC if sort[1] else SORT_ASC)
                hh.setSortIndicatorShown(True)
        except Exception:
            pass

    def _review_sort_by(self, col, *args):
        """A column heading was clicked: sort by that column, ascending; the
        same heading again reverses the order. The order is kept when the
        list is rebuilt (an option ticked, a new run)."""
        try:
            col = int(col)
        except (TypeError, ValueError):
            return
        if not 0 <= col < len(REVIEW_COLS):
            return
        if getattr(self, "_busy", False) and getattr(self, "_task", None) is not None:
            self._review_show_sort()          # Qt moved the arrow on the click: put it back
            return
        cur = getattr(self, "_review_sort", None)
        self._review_sort = (col, (not cur[1]) if cur and cur[0] == col else False)
        self._review_show_sort()
        if getattr(self, "_last_run", None):
            self._populate_review(True)       # with an argument: unsaved edits are kept

    def _review_decisions(self):
        out = []
        t = self.review_table
        for i, (rec, _why) in enumerate(self._review_rows):
            cat = t.cellWidget(i, C_SET_CAT).currentData()
            typ = t.cellWidget(i, C_SET_TYPE).currentData()
            pers = t.cellWidget(i, C_SET_PERS).value()
            note = t.cellWidget(i, C_NOTE).text().strip()
            if cat is None and typ is None and pers < 0 and not note:
                continue
            out.append((i, rec, cat, typ, None if pers < 0 else pers, note))
        return out

    def _review_save(self, *args):
        """Saves the decisions; returns their number, or None when nothing
        could be saved."""
        res = getattr(self, "_last_run", None)
        if not res:
            QMessageBox.warning(self, "Review", "Run Hazard Classification first.")
            return None
        store = ReviewStore(res["review_file"])
        ctx = QgsProject.instance().transformContext()
        decisions = self._review_decisions()
        saved = removed = kept = 0
        done_rows = []
        for i, rec, cat, typ, pers, note in decisions:
            if cat == _CLEAR:
                removed += store.remove_decision(rec, res["crs"], ctx)
                done_rows.append((i, "decision removed - applies on the next run"))
                continue
            if _unchanged(store.decision_at(rec, res["crs"], ctx), cat, typ, pers, note):
                kept += 1                     # keeps the original reviewer and date
                continue
            store.set_decision(rec, res["crs"], ctx, category=cat, acer_type=typ,
                               persons=pers, note=note)
            saved += 1
            done_rows.append((i, "saved - applied on the next run"))
        try:
            store.save()
        except RuntimeError as e:
            QMessageBox.warning(self, "Review", str(e))
            return None
        for i, text in done_rows:
            self.review_table.setItem(i, C_STATUS, QTableWidgetItem(text))
        msg = "{} decision(s) saved{}{} to {}".format(
            saved, ", {} removed".format(removed) if removed else "",
            ", {} unchanged".format(kept) if kept else "", store.path)
        self.review_status.setText(msg)
        self._log(">>> Review: " + msg)
        return saved + removed

    # ---------------------------------------------- v2.8: export / merge --
    def _review_file(self):
        res = getattr(self, "_last_run", None) or {}
        path = res.get("review_file")
        if not path and getattr(self, "config_output_folder", ""):
            path = review_path(self.config_output_folder)
        return path

    def _review_dam_name(self):
        res = getattr(self, "_last_run", None) or {}
        return res.get("dam_name") or None

    def _review_export(self, *args):
        """Portable copy of the saved decisions for another engineer."""
        from . import review_exchange as RX
        if getattr(self, "_busy", False):
            return None
        path = self._review_file()
        if not path or not os.path.isfile(path):
            QMessageBox.information(self, "Review", "There are no saved decisions yet - save "
                                    "the decisions first.")
            return None
        try:
            import getpass
            default_name = getpass.getuser()
        except Exception:
            default_name = ""
        who, ok = QInputDialog.getText(self, "Export decisions", "Your name (recorded with "
                                       "each decision you have not named):", text=default_name)
        if not ok:
            return None
        who = (who or "").strip()
        start = os.path.join(os.path.dirname(path), "Review_Decisions_{}.csv".format(
            "".join(c if c.isalnum() else "_" for c in who) or "export"))
        dest, _f = QFileDialog.getSaveFileName(self, "Export the review decisions", start,
                                               "CSV (*.csv)")
        if not dest:
            return None
        try:
            res = RX.export(path, dest, who, dam_name=self._review_dam_name(),
                            ctx=QgsProject.instance().transformContext())
        except Exception as e:
            log_message("Review export failed: {}".format(e))
            QMessageBox.warning(self, "Review", "The decisions could not be exported: "
                                "{}".format(e))
            return None
        note = ""
        if res["redirected"]:
            note = " (a file was open in another program - written as {})".format(
                os.path.basename(res["csv"]))
        msg = "{} decision(s) exported to {}{}".format(res["rows"], res["csv"], note)
        self.review_status.setText(msg)
        self._log(">>> Review: " + msg)
        QMessageBox.information(self, "Review", msg + "\n\nSend the .csv and its "
                                ".header.json together.")
        return res

    def _review_merge(self, *args):
        """Merge another engineer's exported decisions: plan first (nothing is
        written), the engineer settles the conflicts, then a backup and the merge."""
        from . import review_exchange as RX
        if getattr(self, "_busy", False):
            return None                   # a run reads the decisions file: not while it works
        path = self._review_file()
        if not path:
            QMessageBox.information(self, "Review", "Pick the dam folder first.")
            return None
        theirs, _f = QFileDialog.getOpenFileName(
            self, "Decisions exported by the other engineer", os.path.dirname(path),
            "CSV (*.csv);;All Files (*)")
        if not theirs:
            return None
        ctx = QgsProject.instance().transformContext()
        res = getattr(self, "_last_run", None) or {}
        crs = res.get("crs")
        crs_id = crs.authid() if crs is not None and hasattr(crs, "authid") else None
        if getattr(self, "_last_run", None) and self._review_decisions():
            # decisions typed into the table but not saved would be lost when the list is
            # rebuilt after the merge (and could overwrite merged rows on the next save)
            self._log(">>> Review: saving the decisions in the table before the merge")
            if self._review_save() is None:
                return None
        try:
            store = ReviewStore(path)
            plan = RX.import_plan(store, theirs, dam_name=self._review_dam_name(), crs=crs_id,
                                  ctx=ctx)
            allow = {}
            if plan.refused:
                # two refusals can be overridden by an engineer who knows the file is right:
                # a folder that was renamed, or a file that was edited after the export
                texts = [t for lv, t in plan.issues if lv == "error"]
                allow = {"allow_other_dam": any(t.startswith("the file is for another dam")
                                                for t in texts),
                         "accept_modified": any(t.startswith("the rows do not match")
                                                for t in texts)}
                if (allow["allow_other_dam"] or allow["accept_modified"]) and \
                        len(texts) == sum(allow.values()) and QMessageBox.question(
                            self, "Review merge", "\n".join(RX.plan_lines(plan)) +
                            "\n\nCompare it anyway? Do this only when you know the file is "
                            "the right one (a renamed dam folder, or a file you edited on "
                            "purpose).", MB_YES | MB_NO) == MB_YES:
                    plan = RX.import_plan(store, theirs, dam_name=self._review_dam_name(),
                                          crs=crs_id, ctx=ctx, **allow)
        except Exception as e:
            log_message("Review merge plan failed: {}".format(e))
            QMessageBox.warning(self, "Review", "The file could not be compared: {}".format(e))
            return None
        lines = RX.plan_lines(plan)
        for line in lines:
            self._log(">>> " + line)
        if plan.refused:
            QMessageBox.warning(self, "Review merge", "\n".join(lines))
            return None
        counts = plan.counts()
        if not any(counts[k] for k in (RX.KIND_ADD, RX.KIND_UPDATE, RX.KIND_CONFLICT,
                                       RX.KIND_THEIRS_REMOVED, RX.KIND_MINE_REMOVED)):
            QMessageBox.information(self, "Review merge", "\n".join(lines))
            return plan
        text = "\n".join(lines)
        conflicts = plan.conflicts()
        policy = RX.POLICY_MINE
        if conflicts:
            shown = RX.conflict_lines(plan)
            text += "\n\n" + "\n".join(shown[:24]) + (
                "\n... ({} more in the log)".format(len(shown) - 24) if len(shown) > 24 else "")
            for line in shown:
                self._log(">>>   " + line)
        if QMessageBox.question(self, "Review merge", text + "\n\nMerge now? A backup copy "
                                "of your decisions is made first.",
                                MB_YES | MB_NO) != MB_YES:
            return None
        if conflicts:
            labels = [("Keep mine for every conflict", RX.POLICY_MINE),
                      ("Take theirs for every conflict", RX.POLICY_THEIRS),
                      ("The newest date wins", RX.POLICY_NEWEST)]
            got, ok = QInputDialog.getItem(self, "Review merge", "{} conflict(s): how should "
                                           "they be settled?".format(len(conflicts)),
                                           [t for t, _v in labels], 0, False)
            if not ok:
                return None
            policy = dict(labels).get(got, RX.POLICY_MINE)
        try:
            result = RX.apply_plan(store, plan, policy=policy, ctx=ctx)
        except Exception as e:
            log_message("Review merge failed: {}".format(e))
            QMessageBox.warning(self, "Review", "Nothing was merged: {}".format(e))
            return None
        out = RX.result_lines(result)
        for line in out:
            self._log(">>> " + line)
        self.review_status.setText(out[0])
        if hasattr(self, "_populate_review"):
            self._populate_review()
        QMessageBox.information(self, "Review merge", "\n".join(out) + "\n\nRe-run the Hazard "
                                "Classification to apply the merged decisions.")
        return result

    def _review_save_and_rerun(self, *args):
        if self._review_save() is None:
            return
        self._on_run_classification()

    def _aerial_toggled(self, on):
        """Ticking the box shows the imagery (adding it when a run is loaded);
        unticking hides it."""
        try:
            from . import basemap
            if on:
                if getattr(self, "_last_run", None) and not getattr(self, "_restoring", False):
                    self._aerial_ensure()
                basemap.set_visible(True)
            else:
                basemap.set_visible(False)
        except Exception as e:
            log_message("Aerial imagery: {}".format(e))

    def _aerial_ensure(self):
        """Adds the imagery layer once (before a zoom); never raises."""
        if not self.review_aerial_check.isChecked():
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

    def _review_zoom(self, *args):
        if getattr(self, "_busy", False) and getattr(self, "_task", None) is not None:
            return                    # a background run is changing the structures
        r = self.review_table.currentRow()
        if args and isinstance(args[0], int) and not isinstance(args[0], bool):
            r = args[0]                       # double-clicked row (clicked() sends a bool)
        if r is None or r < 0 or r >= len(self._review_rows):
            return
        rec = self._review_rows[r][0]
        try:
            from qgis.utils import iface
            canvas = iface.mapCanvas()
        except Exception:
            canvas = None
        if canvas is None:
            QMessageBox.information(self, "Review", "The map canvas is not available.")
            return
        self._aerial_ensure()
        try:
            crs = self._last_run["crs"]
            dest = canvas.mapSettings().destinationCrs()
            xf = QgsCoordinateTransform(crs, dest, QgsProject.instance().transformContext())
            bb = QgsRectangle(rec["geom"].boundingBox())
            # a point structure has an empty box: pad by at least 150 ft
            pad = max(bb.width() * 3.0, bb.height() * 3.0,
                      buffer_distance_in_layer_units(150.0, crs))
            bb = QgsRectangle(bb.xMinimum() - pad, bb.yMinimum() - pad, bb.xMaximum() + pad,
                              bb.yMaximum() + pad)
            canvas.setExtent(bb if xf.isShortCircuited() else xf.transformBoundingBox(bb))
            canvas.refresh()
            try:
                canvas.flashGeometries([rec["geom"]], crs)
            except Exception:
                pass
        except Exception as e:
            log_message("Zoom failed: {}".format(e))

    # ------------------------------------------------------ v2.9: web map --
    def _review_open_webmap(self, *args):
        """Opens the selected structure's location in the web browser
        (webmap.py builds the address). Returns the address, or None."""
        if getattr(self, "_busy", False) and getattr(self, "_task", None) is not None:
            return None                   # a background run is changing the structures
        res = getattr(self, "_last_run", None)
        r = self.review_table.currentRow()
        rows = getattr(self, "_review_rows", []) or []
        if not res or r is None or r < 0 or r >= len(rows):
            QMessageBox.information(self, "Review", "Select a structure in the list first.")
            return None
        rec = rows[r][0]
        try:
            url = webmap.url_of_structure(rec, res["crs"],
                                          QgsProject.instance().transformContext())
        except Exception as e:
            log_message("Web map: {}".format(e))
            url = None
        if not url:
            QMessageBox.information(self, "Review", "The location of structure {} could not "
                                    "be converted to latitude / longitude.".format(rec["id"]))
            return None
        ok = QDesktopServices.openUrl(QUrl(url))
        if ok is False:
            QMessageBox.warning(self, "Review", "The web browser could not be started. Open "
                                "this address yourself:\n{}".format(url))
        self._log(">>> Review: structure {} opened in the web map: {}".format(rec["id"], url))
        return url
