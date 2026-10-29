"""QGIS dock that reads a finished dam folder. It does not run the model.

Not wired into initGui. To show it, in the Runner plugin.py initGui::

    from qgis.PyQt.QtCore import Qt
    from .results_dock import ResultsDock
    self.results_dock = ResultsDock(self.iface)
    self.iface.addDockWidget(Qt.RightDockWidgetArea, self.results_dock)

And in unload::

    self.iface.removeDockWidget(self.results_dock)
    self.results_dock.deleteLater()

Works in DamSafety_Runner and nd_breach_runner. The dock looks for
``_breach_model/run_report.json``, ``receptors_flooded.csv`` and
``breach_hydrograph.csv``. "Open dashboard" writes dashboard.html with
``core/dashboard_html.py`` if that module is installed, then opens it.

The numbers are the run's own summary. This dock does not classify hazard.
"""
import csv
import json
import os

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


def _load_json(path):
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _find_up(start, name, levels=4):
    folder = start
    for _ in range(levels):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return None


def _summary(report):
    block = report.get("summary")
    if isinstance(block, dict) and block:
        return block
    return report if isinstance(report, dict) else {}


def _hydro_peak(path):
    peak = None
    rows = 0
    if not path:
        return peak, rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = [p.strip() for p in line.replace(";", ",").split(",")]
            if len(parts) <= 1:
                continue
            try:
                q = float(parts[1])
            except ValueError:
                continue
            rows += 1
            peak = q if peak is None else max(peak, q)
    return peak, rows


def _cell(value, digits=1):
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        return "{:.{d}f}".format(float(text), d=digits)
    except ValueError:
        return text


def _receptors(path):
    if not path:
        return []
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []
        fields = {n.lower().strip(): n for n in reader.fieldnames if n}

        def col(*names):
            for n in names:
                if n in fields:
                    return fields[n]
            return None

        name_k = col("id", "name", "structure", "feature", "label")
        kind_k = col("kind", "type", "layer")
        depth_k = col("depth_ft", "depth", "max_depth_ft")
        vel_k = col("velocity_fps", "velocity", "vel_fps")
        arr_k = col("arrival_hr", "arrival", "arrival_h")
        rows = []
        for row in reader:
            rows.append([
                (row.get(name_k) or "").strip() if name_k else "",
                (row.get(kind_k) or "").strip() if kind_k else "",
                _cell(row.get(depth_k) if depth_k else None),
                _cell(row.get(vel_k) if vel_k else None),
                _cell(row.get(arr_k) if arr_k else None),
            ])
    return rows


class ResultsDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Breach results", parent or iface.mainWindow())
        self.iface = iface
        self.setObjectName("NdBreachResultsDock")
        self._folder = None
        self._model_dir = None
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        root = QWidget()
        lay = QVBoxLayout(root)
        self.summary = QLabel("Open a dam folder. The dock only reads files already written.")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

        buttons = QHBoxLayout()
        open_btn = QPushButton("Dam folder…")
        open_btn.clicked.connect(self.choose_folder)
        dash_btn = QPushButton("Open dashboard")
        dash_btn.clicked.connect(self.open_dashboard)
        buttons.addWidget(open_btn)
        buttons.addWidget(dash_btn)
        lay.addLayout(buttons)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Feature", "Kind", "Depth ft", "Vel ft/s", "Arrival h"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        lay.addWidget(self.table)
        self.setWidget(root)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Dam folder or _breach_model")
        if folder:
            self.set_folder(folder)

    def set_folder(self, folder):
        self._folder = folder
        report_path = None
        if os.path.basename(folder) == "run_report.json":
            report_path = folder
        else:
            report_path = _find_up(folder, os.path.join("_breach_model", "run_report.json"))
            if report_path is None:
                report_path = _find_up(folder, "run_report.json")
        model_dir = os.path.dirname(report_path) if report_path else folder
        report = _load_json(report_path) if report_path else {}
        summary = _summary(report)
        dam = report.get("dam") if isinstance(report.get("dam"), dict) else {}
        hydro_path = _find_up(model_dir, "breach_hydrograph.csv")
        peak, nrows = _hydro_peak(hydro_path)
        if summary.get("peak_breach_cfs") is not None:
            try:
                peak = float(summary["peak_breach_cfs"])
            except (TypeError, ValueError):
                pass
        rec_path = _find_up(model_dir, "receptors_flooded.csv")
        rows = _receptors(rec_path)
        dam_id = summary.get("dam_id") or dam.get("dam_id") or os.path.basename(
            os.path.dirname(model_dir))
        bits = [str(dam_id)]
        if dam.get("name"):
            bits.append(str(dam["name"]))
        if summary.get("scenario"):
            bits.append(str(summary["scenario"]))
        method = summary.get("breach_method")
        if method:
            bits.append(str(method))
        solver = summary.get("final_model") or report.get("flood_engine_short")
        if solver:
            bits.append(str(solver))
        if peak is not None:
            bits.append("peak {:.0f} cfs".format(peak))
        if summary.get("flooded_acres") is not None:
            bits.append("{:.0f} ac".format(float(summary["flooded_acres"])))
        if summary.get("max_depth_ft") is not None:
            bits.append("max depth {:.1f} ft".format(float(summary["max_depth_ft"])))
        if summary.get("receptors_flooded"):
            bits.append(str(summary["receptors_flooded"]))
        warns = report.get("warnings") or []
        if warns:
            bits.append("{} thing(s) to check".format(len(warns)))
        if not report_path:
            bits.append("no run_report.json in this folder")
        if hydro_path and nrows:
            bits.append("{} hydrograph rows".format(nrows))
        self.summary.setText(" · ".join(bits))
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                self.table.setItem(r, c, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
        self._model_dir = model_dir

    def open_dashboard(self):
        folder = self._model_dir or self._folder
        if not folder:
            self.choose_folder()
            folder = self._model_dir
        if not folder:
            return
        path = os.path.join(folder, "dashboard.html")
        write_dashboard = _load_writer()
        if write_dashboard is not None:
            path = write_dashboard(folder)
        if os.path.isfile(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            self.summary.setText(
                "No dashboard.html yet. Add core/dashboard_html.py so this button can write it."
            )


def _load_writer():
    try:
        from .core.dashboard_html import write_dashboard
        return write_dashboard
    except Exception:
        pass
    try:
        from .dashboard_html import write_dashboard
        return write_dashboard
    except Exception:
        return None
