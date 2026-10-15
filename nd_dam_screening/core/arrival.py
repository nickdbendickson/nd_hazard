"""How long the people downstream have (0.28.0): arrival-time contours and a warning time.

Step 7 already writes an arrival raster per dam (<dam>_<scenario>_arrive.tif, hours since the
breach) and 0.26.0 put "the wet area reached within 0.5 / 1 / 2 / 4 / 8 h" in the flood polygons.
What was missing is the pair of things an emergency action plan is written from:

  * the CONTOURS - 30 minutes, 1 hour, 2 hours - as a line layer, so a map shows where the front
    is at the times a siren and a phone tree are measured against;
  * a WARNING TIME per structure, in the bands the loss-of-life literature uses, next to that
    structure's depth and velocity in the impacted-features layer.

The bands are damsuite.loss.warning_band (under 15 minutes / 15 to 60 / over 60), so the Runner's
loss-of-life work and this screening put the same structure in the same band. The warning time is
taken as the flood's ARRIVAL time: the dam fails at minute zero and nobody is warned before it.
That is the conservative reading and the one the literature's rate tables are built on; a real
plan with gauges and a siren gives more, and an engineer who knows the real warning time should
say so rather than take this number.

    hours = sample(arrive_h, gt, x, y)        # hours at a point, or None off the grid / dry
    band  = band_of(hours)                    # "under15" | "15to60" | "over60" | ""
    text  = band_text(hours)                  # "23 min (15 to 60 minutes)"
    masks = contour_masks(arrive_h, wet)      # [(label, hours, mask)] for the contour layer

The masks are pure numpy so they are tested without GDAL; turning one into a line is
core/flood_polygons.py, which already owns the GDAL side.
"""
import numpy as np

NODATA = -9999.0
# the contour times, in hours (30 min, 1 h, 2 h): the first is the one that decides whether an
# EAP can do anything at all
CONTOUR_HOURS = (0.5, 1.0, 2.0)
CONTOUR_LABEL = {0.5: "30 min", 1.0: "1 h", 2.0: "2 h"}
LAYER_CONTOUR = "arrival_contour"


def _loss():
    """damsuite.loss - the suite's warning-time bands. Imported here, not at the top, so the
    contour side of this module also works when core/ is read as a plain folder (the GDAL part
    of core/flood_polygons.py is tested that way)."""
    from ..damsuite import loss as LS
    return LS


def bands():
    return _loss().WARNING_BANDS


def band_labels():
    return _loss().WARNING_LABELS


def label_for(hours):
    h = float(hours)
    return CONTOUR_LABEL.get(h, "{:g} h".format(h))


def sample(arrive_h, gt, x, y, nodata=NODATA):
    """The arrival time (hours) of the flood at a point, or None when the point is off the grid
    or the cell never got wet. `gt` is a north-up GDAL geotransform."""
    if arrive_h is None or gt is None:
        return None
    a = np.asarray(arrive_h, float)
    if a.ndim != 2 or not a.size:
        return None
    cell = float(gt[1])
    if cell <= 0:
        return None
    col = int((float(x) - float(gt[0])) / cell)
    row = int((float(gt[3]) - float(y)) / abs(float(gt[5]) or cell))
    if row < 0 or col < 0 or row >= a.shape[0] or col >= a.shape[1]:
        return None
    v = a[row, col]
    if not np.isfinite(v) or v <= nodata + 1 or v < 0:
        return None
    return float(v)


def minutes(hours):
    return None if hours is None else float(hours) * 60.0


def band_of(hours):
    """The warning band of an arrival time in HOURS ("" when there is none)."""
    m = minutes(hours)
    return _loss().warning_band(m) if m is not None else ""


def band_label(hours):
    return band_labels().get(band_of(hours), "")


def band_text(hours):
    """"23 min (15 to 60 minutes)" - the impacted layer's warn_time column, and the report's."""
    m = minutes(hours)
    if m is None:
        return ""
    shown = "{:.0f} min".format(m) if m < 180 else "{:.1f} h".format(m / 60.0)
    return "{} ({})".format(shown, band_label(hours))


def contour_masks(arrive_h, wet, hours=CONTOUR_HOURS, nodata=NODATA):
    """[(label, hours, mask)] - the wet cells the flood reaches within each contour time. The
    boundary of each mask is the contour; an hour nothing is reached by is left out."""
    a = np.asarray(arrive_h, float)
    w = np.asarray(wet, bool)
    valid = np.isfinite(a) & (a > nodata + 1) & (a >= 0) & w
    out = []
    for h in hours:
        m = valid & (a <= float(h))
        if m.any():
            out.append((label_for(h), float(h), m))
    return out


def worst_band(hours_list):
    """The shortest warning time among a dam's flooded structures ("" when there is none) - the
    one line a dam's report should carry."""
    vals = [h for h in (hours_list or []) if h is not None]
    if not vals:
        return ""
    return band_of(min(vals))


def summary_text(hours_list):
    """"soonest 12 min (under 15 minutes) at 27 structure(s)" for the dam's notes."""
    vals = sorted(h for h in (hours_list or []) if h is not None)
    if not vals:
        return ""
    return "soonest {} at {} flooded feature(s)".format(band_text(vals[0]), len(vals))
