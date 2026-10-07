# -*- coding: utf-8 -*-
"""Simple categorized styles for the classification outputs."""

from qgis.PyQt.QtGui import QColor
from qgis.core import QgsSymbol, QgsRendererCategory, QgsCategorizedSymbolRenderer

from .compat import GEOM_LINE, MSG_INFO
from .helpers import log_message

COLORS = {
    "HIGH": "#F44336", "JUDGMENT": "#FFEB3B", "LOW": "#4CAF50", "DRY": "#9E9E9E",
    "High": "#F44336", "Medium": "#FF9800", "Low": "#4CAF50",
    # sample locations (where the values were read)
    "Hmax_ft": "#08519C", "Vmax_ftps": "#D94801", "Arrival_hr": "#238B45",
    "EdgeBuf_Hmax_ft": "#6BAED6", "EdgeBuf_Vmax_ftps": "#FD8D3C",
    "Max depth": "#08519C", "Max velocity": "#D94801", "Max depth x velocity": "#7B3294",
    "Earliest arrival": "#238B45", "Controlling ACER zone": "#E7298A",
}


def style_by_field(layer, field):
    """Categorized renderer on `field` using the ACER / ND colours. Never
    raises - a styling problem must not fail the run."""
    try:
        idx = layer.fields().indexOf(field)
        if idx < 0:
            return False
        cats = []
        for v in sorted(layer.uniqueValues(idx), key=str):
            sym = QgsSymbol.defaultSymbol(layer.geometryType())
            if sym is None:
                continue
            sym.setColor(QColor(COLORS.get(str(v), "#607D8B")))
            if layer.geometryType() == GEOM_LINE and hasattr(sym, "setWidth"):
                sym.setWidth(1.2)
            cats.append(QgsRendererCategory(v, sym, str(v)))
        if not cats:
            return False
        layer.setRenderer(QgsCategorizedSymbolRenderer(field, cats))
        layer.triggerRepaint()
        return True
    except Exception as e:
        log_message("Could not style {}: {}".format(layer.name(), e), MSG_INFO)
        return False


def style_depth(layer, max_ft=None):
    """Light-to-dark blue ramp for a depth raster (for maps and the report).
    Never raises."""
    try:
        from qgis.core import (QgsColorRampShader, QgsRasterShader,
                               QgsSingleBandPseudoColorRenderer)
        from .compat import SHADER_LINEAR
        top = float(max_ft) if max_ft else 10.0
        items = [QgsColorRampShader.ColorRampItem(0.0, QColor("#c6dbef"), "0 ft"),
                 QgsColorRampShader.ColorRampItem(top / 2.0, QColor("#4292c6"),
                                                  "{:g} ft".format(top / 2.0)),
                 QgsColorRampShader.ColorRampItem(top, QColor("#08306b"),
                                                  ">= {:g} ft".format(top))]
        ramp = QgsColorRampShader()
        if SHADER_LINEAR is not None:
            ramp.setColorRampType(SHADER_LINEAR)
        ramp.setColorRampItemList(items)
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(ramp)
        layer.setRenderer(QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader))
        layer.triggerRepaint()
        return True
    except Exception as e:
        log_message("Could not style {}: {}".format(layer.name(), e), MSG_INFO)
        return False


def style_outline(layer, color="#E7298A", width_mm=0.6, fill_alpha=0):
    """Hollow outline for an area layer (evaluation areas, sampled cells):
    coloured border, no or faint fill. Never raises."""
    try:
        from qgis.core import QgsFillSymbol, QgsSingleSymbolRenderer
        c = QColor(color)
        fill = QColor(c)
        fill.setAlpha(int(fill_alpha))
        sym = QgsFillSymbol.createSimple({
            "color": "{},{},{},{}".format(fill.red(), fill.green(), fill.blue(), fill.alpha()),
            "outline_color": "{},{},{},255".format(c.red(), c.green(), c.blue()),
            "outline_width": "{:.2f}".format(width_mm), "outline_style": "solid",
            "style": "solid" if fill_alpha else "no"})
        if sym is None:
            return False
        layer.setRenderer(QgsSingleSymbolRenderer(sym))
        layer.triggerRepaint()
        return True
    except Exception as e:
        log_message("Could not style {}: {}".format(layer.name(), e), MSG_INFO)
        return False


MAP_TIPS = {
    "Habitable_Structures": (
        '"Struct_ID"',
        '<b>Structure [% "Struct_ID" %]</b> ([% "Sources" %])<br>'
        'Habitable: [% "Habitable" %] - ACER type: [% "ACER_Type" %]<br>'
        'FEMA category: [% "FEMA_Category" %]<br>'
        'ACER zone: <b>[% "ACER_Zone_Label" %]</b> - ND outcome: <b>[% "ND_Outcome" %]</b><br>'
        'Depth [% round("Max_Depth_ft", 1) %] ft, velocity [% round("Max_Vel_fps", 1) %] '
        'ft/s, arrival [% round("First_Arrival_hr", 2) %] h<br>'
        'Persons [% "Persons" %] ([% "Persons_Basis" %])<br>[% "Reviewed" %]'),
    "Roads_Inundated": (
        '"Road_Name"',
        '<b>[% "Road_Name" %]</b> ([% "Road_Class" %])<br>ND class <b>[% "ND_Class" %]</b>: '
        '[% "ND_Basis" %]<br>Overtopped [% round("Length_ft", 0) %] ft, depth '
        '[% round("Max_Depth_ft", 1) %] ft'),
    "Structure_Sample_Points": (
        '"Struct_ID"',
        '<b>Structure [% "Struct_ID" %]</b><br>[% "Value_Type" %]: [% "Label" %]'),
    "Structure_Evaluation_Areas": (
        '"Struct_ID"',
        '<b>Structure [% "Struct_ID" %]</b> - area searched for max depth / velocity<br>'
        '[% "Area_Type" %] (reach [% round("Reach_ft", 1) %] ft)<br>'
        '[% "Wet_Cells" %] wet cell(s): [% "Sampling" %]<br>'
        'Depth [% round("Max_Depth_ft", 2) %] ft, velocity [% round("Max_Vel_fps", 2) %] '
        'ft/s - ACER zone <b>[% "ACER_Zone_Label" %]</b>, ND outcome [% "ND_Outcome" %]'),
    "Structure_Sampled_Cells": (
        '"Struct_ID"',
        '<b>Structure [% "Struct_ID" %]</b>: [% "Wet_Cells" %] wet cell(s) read<br>'
        'Depth [% round("Max_Depth_ft", 2) %] ft, velocity [% round("Max_Vel_fps", 2) %] '
        'ft/s - [% "ACER_Zone_Label" %]'),
    "Dams_In_Series": (
        '"Dam_Name"',
        '<b>[% "Dam_Name" %]</b> [% "NID_ID" %]<br>Hazard potential: [% "Hazard_Potential" %]'
        '<br>Flooded: [% "Flooded" %], depth [% round("Max_Depth_ft", 1) %] ft'),
    "Lifeline_Facilities": (
        '"Facility"',
        '<b>[% "Facility" %]</b> ([% "Category" %])<br>Flooded: [% "Flooded" %], depth '
        '[% round("Max_Depth_ft", 1) %] ft<br>[% "Source" %]'),
    "EAP_Notification": (
        '"Address"',
        '<b>#[% "Order" %] [% "Address" %]</b><br>[% "Type" %] - [% "Category" %]<br>'
        'Arrival [% "Arrival_hhmm" %], depth [% round("Max_Depth_ft", 1) %] ft'),
    "Model_Boundary_Check": (
        '"Side"',
        '<b>Wet cell on the model edge ([% "Side" %])</b><br>depth '
        '[% round("Depth_ft", 1) %] ft, incremental [% round("Incr_ft", 1) %] ft'),
    "FEMA_Structures_Sample_Points": (
        '"Outline_FID"',
        '<b>[% "Value_Type" %]</b> = [% round("Value", 2) %]<br>[% "Method" %]'
        '<br>offset [% round("Offset_ft", 1) %] ft'),
}


def set_map_tip(layer, kind):
    """Identify / map-tip text for a plugin layer (View > Show Map Tips).
    Fields the layer does not have are simply left empty. Never raises."""
    try:
        key = next((k for k in MAP_TIPS if k in kind), None)
        if key is None:
            return False
        display, tip = MAP_TIPS[key]
        layer.setDisplayExpression(display)
        layer.setMapTipTemplate(tip)
        return True
    except Exception as e:
        log_message("Map tip not set on {}: {}".format(kind, e), MSG_INFO)
        return False
