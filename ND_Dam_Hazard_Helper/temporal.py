# -*- coding: utf-8 -*-
"""v2.9: the flood arrival played in the QGIS Temporal Controller.

What plays. The vector layers that carry a flood time after breach start:

  * EAP_Arrival_Contours (field "Arrival_hr", hours; eap.write_arrival_contours,
    GeoPackage layer "arrival_contours" - one line per arrival break),
  * EAP_Notification_List (field "Arrival_hr", hours; eap.notification_layer),
  * EAP_Road_Closures (field "Close_min", MINUTES; evacuation.closures_layer).

Each feature becomes visible at breach start + its time and stays visible, so
the flood front sweeps downstream frame by frame. The EAP_Arrival_Bands
raster is NOT animated: a raster layer's temporal settings give one time range
for the whole layer in QGIS 3.28 (per-pixel time values arrived only in 3.38,
and on an hours raster, not on the band classes), so it stays static under the
contours, which carry the same breaks (EAP_ARRIVAL_BREAKS_HR).

How. The vector layer temporal mode "start and end from expressions" with only
a START expression, e.g.

    make_datetime(2000,1,1,0,0,0) + make_interval(seconds:=coalesce("Arrival_hr", 0) * 3600)

For that mode QGIS filters each frame with "(start expression) <= frame end"
(QgsVectorLayerTemporalProperties::createFilterString - the same in 3.28 and
4.x), i.e. a feature is shown from its time onward. A field-based mode is not
used: "instant from field" / "start and end from fields" need a date-time
field and these fields are numbers (hours / minutes); the expression needs no
new field, nothing is written to the GeoPackage, and the settings are kept in
the QGIS project with the layer. The mode enum is resolved defensively, as in
compat.py: Qgis.VectorTemporalMode (3.22+, the only spelling in QGIS 4) first,
then QgsVectorLayerTemporalProperties.TemporalMode / the unscoped Mode...
names of older builds.

Breach start - NOMINAL. The rasters give hours after breach start, not a date.
The default is 2000-01-01 00:00 local time (NOMINAL_BREACH_START): a fixed,
obviously nominal date, so the Temporal Controller's clock reads the elapsed
time (01-01 02:30 = 2 h 30 min after breach start; 01-02 = one day later), the
project is the same on every run, and January has no daylight-saving change.
Any other start can be given (QDateTime, datetime, "YYYY-MM-DD HH:MM" text).

set_project_time_range sets the project's temporal range and frame step
(default 15 min - a display choice, not an engineering value) and, called on
the main thread inside QGIS, also the map canvas' Temporal Controller: the
dock reads the project's range only when a project is opened.

Nothing here raises: every function returns (ok, message) or a plain value.
Features without a time are shown from breach start (null_at_start=True, the
cautious choice for a warning list) or never while the animation runs (False).
"""

import datetime as _dt
import math

NOMINAL_BREACH_START = (2000, 1, 1, 0, 0, 0)     # NOMINAL - see the module text
DEFAULT_STEP_MIN = 15.0                          # frame step (display choice)
UNIT_SECONDS = {"hours": 3600.0, "minutes": 60.0, "seconds": 1.0}

# (layer name, field, units) of the plugin's layers that can be animated
ANIMATED_LAYERS = (("EAP_Arrival_Contours", "Arrival_hr", "hours"),
                   ("EAP_Notification_List", "Arrival_hr", "hours"),
                   ("EAP_Road_Closures", "Close_min", "minutes"))


# ------------------------------------------------------------ enum shims --
def _first(*getters):
    for g in getters:
        try:
            v = g()
        except Exception:
            continue
        if v is not None:
            return v
    return None


def expression_mode():
    """The "start and end from expressions" vector temporal mode of this QGIS
    build (None when it has none)."""
    from qgis.core import Qgis
    try:
        from qgis.core import QgsVectorLayerTemporalProperties as P
    except ImportError:
        P = None
    return _first(lambda: Qgis.VectorTemporalMode.FeatureDateTimeStartAndEndFromExpressions,
                  lambda: P.TemporalMode.ModeFeatureDateTimeStartAndEndFromExpressions,
                  lambda: P.ModeFeatureDateTimeStartAndEndFromExpressions)


def minutes_unit():
    """The "minutes" temporal unit (Qgis.TemporalUnit 3.30+ / QgsUnitTypes older)."""
    from qgis.core import Qgis, QgsUnitTypes
    return _first(lambda: Qgis.TemporalUnit.Minutes,
                  lambda: QgsUnitTypes.TemporalUnit.TemporalMinutes,
                  lambda: QgsUnitTypes.TemporalMinutes)


def animated_navigation_mode():
    """The Temporal Controller's "animated" navigation mode (None if unknown)."""
    from qgis.core import Qgis
    try:
        from qgis.core import QgsTemporalNavigationObject as N
    except ImportError:
        N = None
    return _first(lambda: Qgis.TemporalNavigationMode.Animated,
                  lambda: N.NavigationMode.Animated, lambda: N.Animated)


# ------------------------------------------------------------ date / time --
def _to_py(value=None):
    """naive datetime.datetime of a breach start / time given as None (the
    nominal start), QDateTime, datetime / date, (y, m, d[, h, mi, s]) or ISO text;
    None when it cannot be read."""
    if value is None:
        return _dt.datetime(*NOMINAL_BREACH_START)
    if isinstance(value, _dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    if isinstance(value, (tuple, list)):
        try:
            return _dt.datetime(*[int(x) for x in value])
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return _dt.datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None
    try:                                          # QDateTime
        if hasattr(value, "isValid") and not value.isValid():
            return None
        d, t = value.date(), value.time()
        return _dt.datetime(d.year(), d.month(), d.day(), t.hour(), t.minute(), t.second())
    except Exception:
        return None


def to_qdatetime(value=None):
    """QDateTime (local time) of `value` (see _to_py); None when unreadable."""
    p = _to_py(value)
    if p is None:
        return None
    try:
        from qgis.PyQt.QtCore import QDate, QDateTime, QTime
        return QDateTime(QDate(p.year, p.month, p.day), QTime(p.hour, p.minute, p.second))
    except Exception:
        return None


def _quote_field(name):
    return '"{}"'.format(str(name).replace('"', '""'))


def arrival_start_expression(field, breach_start=None, units="hours", null_at_start=True):
    """QGIS expression: breach start + the field's value (hours / minutes /
    seconds) as a date-time. Missing values: breach start (null_at_start) or
    NULL (never shown during the animation)."""
    p = _to_py(breach_start) or _dt.datetime(*NOMINAL_BREACH_START)
    k = UNIT_SECONDS.get(units, 3600.0)
    ref = _quote_field(field)
    val = "coalesce({}, 0)".format(ref) if null_at_start else ref
    return "make_datetime({},{},{},{},{},{}) + make_interval(seconds:={} * {:g})".format(
        p.year, p.month, p.day, p.hour, p.minute, p.second, val, k)


# ---------------------------------------------------------------- layers --
def _field_name(layer, field):
    try:
        names = [f.name() for f in layer.fields()]
    except Exception:
        return None
    if field in names:
        return field
    low = [n for n in names if n.lower() == str(field).lower()]   # GPKG / SHP case changes
    return low[0] if low else None


def apply_arrival_animation(layer, hours_field, breach_start=None, units="hours",
                            null_at_start=True):
    """Make `layer` play in the Temporal Controller: each feature active from
    breach start + its `hours_field` value onward (units "hours" - the default
    - "minutes" or "seconds"). breach_start: None = NOMINAL_BREACH_START.
    Returns (ok, message); never raises."""
    name = "layer"
    try:
        from qgis.core import QgsVectorLayer
        if layer is None:
            return False, "no layer to animate"
        name = layer.name() if hasattr(layer, "name") else name
        if not isinstance(layer, QgsVectorLayer):
            return False, "{}: not a vector layer (a raster layer cannot be animated per " \
                          "cell in QGIS 3.28)".format(name)
        if not layer.isValid():
            return False, "{}: the layer is not valid".format(name)
        fld = _field_name(layer, hours_field)
        if fld is None:
            return False, "{}: no field {}".format(name, hours_field)
        if _to_py(breach_start) is None:
            return False, "{}: breach start not understood: {!r}".format(name, breach_start)
        try:
            props = layer.temporalProperties()
        except Exception:
            props = None
        mode = expression_mode()
        if props is None or mode is None:
            return False, "{}: this QGIS build has no expression-based vector temporal " \
                          "mode - not animated".format(name)
        expr = arrival_start_expression(fld, breach_start, units, null_at_start)
        props.setMode(mode)
        props.setStartExpression(expr)
        props.setEndExpression("")
        props.setIsActive(True)
        try:
            layer.triggerRepaint()
        except Exception:
            pass
        start = _to_py(breach_start)
        return True, "{}: each feature shown from breach start + {} ({}); breach start {} " \
                     "(nominal)".format(name, fld, units,
                                        start.strftime("%Y-%m-%d %H:%M"))
    except Exception as e:                          # noqa: BLE001 - never raises
        return False, "{}: animation not set: {}".format(name, e)


def clear_animation(layer):
    """Switch a layer's temporal filtering off again; (ok, message)."""
    try:
        layer.temporalProperties().setIsActive(False)
        return True, "{}: temporal filtering off".format(layer.name())
    except Exception as e:                          # noqa: BLE001
        return False, "animation not cleared: {}".format(e)


def max_field_value(layer, field):
    """Largest finite value of a numeric field (None when there is none)."""
    try:
        fld = _field_name(layer, field)
        if fld is None:
            return None
        idx = layer.fields().indexOf(fld)
        best = None
        for f in layer.getFeatures():
            try:
                v = float(f.attributes()[idx])
            except (TypeError, ValueError, IndexError):
                continue
            if math.isfinite(v) and (best is None or v > best):
                best = v
        return best
    except Exception:
        return None


def time_range_for(max_value, units="hours", breach_start=None, step_minutes=DEFAULT_STEP_MIN):
    """(start, end) QDateTime covering breach start to the last time rounded up
    to a whole frame step (at least one step). (None, None) when unreadable."""
    p = _to_py(breach_start)
    if p is None:
        return None, None
    step = max(float(step_minutes or DEFAULT_STEP_MIN), 1.0 / 60.0)
    mins = 0.0
    try:
        if max_value is not None and math.isfinite(float(max_value)):
            mins = max(0.0, float(max_value) * UNIT_SECONDS.get(units, 3600.0) / 60.0)
    except (TypeError, ValueError):
        mins = 0.0
    n = max(1, int(math.ceil(mins / step - 1e-9)))
    end = p + _dt.timedelta(minutes=n * step)
    return to_qdatetime(p), to_qdatetime(end)


# --------------------------------------------------------------- project --
def _set_controller(canvas, rng, step_minutes, unit, animate):
    """The map canvas' Temporal Controller (QgsTemporalNavigationObject):
    extents, frame duration and - with `animate` - the animated mode."""
    from qgis.core import QgsInterval
    nav = canvas.temporalController()
    if nav is None or not hasattr(nav, "setTemporalExtents"):
        return False, "no temporal controller on the map canvas"
    nav.setTemporalExtents(rng)
    try:
        interval = QgsInterval(float(step_minutes), unit) if unit is not None else \
            QgsInterval(float(step_minutes) * 60.0)
    except Exception:
        interval = QgsInterval(float(step_minutes) * 60.0)
    if hasattr(nav, "setFrameDuration"):
        nav.setFrameDuration(interval)
    if animate:
        mode = animated_navigation_mode()
        if mode is None or not hasattr(nav, "setNavigationMode"):
            return True, "Temporal Controller range set (animation mode not switched on)"
        nav.setNavigationMode(mode)
    return True, "Temporal Controller range set"


def set_project_time_range(start, end, step_minutes=DEFAULT_STEP_MIN, project=None,
                           canvas=None, use_iface=True, animate=False):
    """Project temporal range start..end (QDateTime / datetime / text) with a
    frame step of `step_minutes` (QgsProjectTimeSettings), and the map
    canvas' Temporal Controller when a canvas is given or - use_iface - the
    QGIS interface has one (call on the MAIN thread). animate: also switch
    the controller to its animated mode. Returns (ok, message); never raises."""
    try:
        from qgis.core import QgsDateTimeRange, QgsProject
        ps, pe = _to_py(start), _to_py(end)
        if ps is None or pe is None:
            return False, "time range not set: start / end not understood"
        if pe <= ps:
            return False, "time range not set: the end is not after the start"
        qs, qe = to_qdatetime(ps), to_qdatetime(pe)
        proj = project if project is not None else QgsProject.instance()
        try:
            ts = proj.timeSettings()
        except Exception:
            return False, "this QGIS build has no project time settings"
        rng = QgsDateTimeRange(qs, qe)
        ts.setTemporalRange(rng)
        step = max(float(step_minutes or DEFAULT_STEP_MIN), 1.0 / 60.0)
        unit = minutes_unit()
        msg = ["project time range {} to {}".format(ps.strftime("%Y-%m-%d %H:%M"),
                                                    pe.strftime("%Y-%m-%d %H:%M"))]
        if unit is not None and hasattr(ts, "setTimeStep") and hasattr(ts, "setTimeStepUnit"):
            ts.setTimeStepUnit(unit)
            ts.setTimeStep(step)
            msg.append("frame step {:g} min".format(step))
        else:
            msg.append("frame step not set (this QGIS build has no project time step)")
        if canvas is None and use_iface:
            try:
                from qgis.utils import iface
                canvas = iface.mapCanvas() if iface is not None else None
            except Exception:
                canvas = None
        if canvas is not None:
            try:
                _ok, m = _set_controller(canvas, rng, step, unit, animate)
            except Exception as e:                  # noqa: BLE001
                m = "Temporal Controller not set: {}".format(e)
            msg.append(m)
        return True, "; ".join(msg)
    except Exception as e:                          # noqa: BLE001 - never raises
        return False, "time range not set: {}".format(e)


def animate_layers(items, breach_start=None, step_minutes=DEFAULT_STEP_MIN, project=None,
                   canvas=None, use_iface=True, null_at_start=True):
    """Convenience for the pipeline: items = [(layer, field, units)] (layers may
    be None). Animates each, then sets the project range from breach start to
    the latest time of all of them. Returns (n_animated, [messages])."""
    msgs, n, latest_min = [], 0, None
    for lyr, field, units in items or ():
        if lyr is None:
            continue
        ok, m = apply_arrival_animation(lyr, field, breach_start, units, null_at_start)
        msgs.append(m)
        if not ok:
            continue
        n += 1
        mx = max_field_value(lyr, field)
        if mx is not None:
            mins = mx * UNIT_SECONDS.get(units, 3600.0) / 60.0
            latest_min = mins if latest_min is None else max(latest_min, mins)
    if n:
        s, e = time_range_for(latest_min, "minutes", breach_start, step_minutes)
        ok, m = set_project_time_range(s, e, step_minutes, project, canvas, use_iface)
        msgs.append(m)
    return n, msgs
