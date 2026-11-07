"""Resolve the top-of-dam elevation and locate the crest on the DEM.

Functions
---------
resolve_top_of_dam   priority cascade: user → inventory → DEM line → DEM point
crest_band_m         half-width of the elevation band that counts as crest
embankment_crest     nearest ground within the crest band (DWR terrain.py)
crest_near           highest ground within tolerance of the top of dam (nd terrain.py)
"""
import math

import numpy as np

FT_M = 0.3048

CREST_BAND_FT = 3.0


# ---------------------------------------------------------------------------
# Top-of-dam resolution (from crest_rule.py)
# ---------------------------------------------------------------------------
def resolve_top_of_dam(user_ft=None, dwr_ft=None, line_median_ft=None, near_ft=None):
    """One top-of-dam number for the screener and the runner.

    Priority: user crest, then inventory crest, then median DEM on the crest
    line, then DEM near the dam point.  Returns (elevation_ft, source_string)
    or (None, 'not resolved').
    """
    def ok(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None
    for v, src in ((user_ft, "as-built crest given by the user"),
                   (dwr_ft, "inventory crest"),
                   (line_median_ft, "median DEM along the crest line"),
                   (near_ft, "DEM near the dam point")):
        n = ok(v)
        if n is not None:
            return n, src
    return None, "not resolved"


# ---------------------------------------------------------------------------
# Crest band (from DWR terrain.py)
# ---------------------------------------------------------------------------
def crest_band_m(dam_height_m):
    """Half-width (m) of the elevation band around the top of dam that counts
    as the crest: at least 3 ft or 10 % of the dam height."""
    return max(CREST_BAND_FT * FT_M, 0.1 * float(dam_height_m or 0.0))


# ---------------------------------------------------------------------------
# Embankment crest (from DWR terrain.py)
# ---------------------------------------------------------------------------
def embankment_crest(grid, dam_xy, dam_height_m, top_m, radius_m=400.0):
    """The crest of THIS dam near a label point.

    Ground within `crest_band_m` of the top of dam is embankment crest: the
    given point is kept when it is on it, else the nearest such cell.  Without
    such ground, the highest ground NOT above the band.  Ground well above the
    top of dam (a hill within the search radius) is never the crest.

    Returns (crest_xy, crest_z_m, note) or (None, None, reason).
    """
    xs = grid.x_centers()
    ys = grid.y_centers()
    X, Y = np.meshgrid(xs, ys)
    z = grid.masked()
    d = np.hypot(X - dam_xy[0], Y - dam_xy[1])
    near = (d <= radius_m) & np.isfinite(z)
    if not near.any():
        return None, None, "no DEM within {:.0f} m of the dam point".format(radius_m)
    band = crest_band_m(dam_height_m)
    on = near & (np.abs(z - top_m) <= band)
    if on.any():
        dd = np.where(on, d, np.inf)
        rr, cc = np.unravel_index(int(np.argmin(dd)), dd.shape)
        if dd[rr, cc] <= max(grid.dx, 1e-9) * 1.5:
            return (float(dam_xy[0]), float(dam_xy[1])), float(z[rr, cc]), \
                "the dam point is on the crest"
        return (float(X[rr, cc]), float(Y[rr, cc])), float(z[rr, cc]), \
            "nearest ground within {:.1f} ft of the top of dam".format(band / FT_M)
    low = near & (z <= top_m + band)
    if not low.any():
        return None, None, ("all ground within {:.0f} m of the dam point is more than {:.1f} ft "
                            "above the top of dam: the point is not at this dam (or the top of "
                            "dam is in another datum)".format(radius_m, band / FT_M))
    k = np.lexsort((d.ravel(), -np.where(low, z, -np.inf).ravel()))[0]
    rr, cc = np.unravel_index(int(k), z.shape)
    return (float(X[rr, cc]), float(Y[rr, cc])), float(z[rr, cc]), \
        "highest ground not above the top of dam (no ground within {:.1f} ft of it)".format(
            band / FT_M)


# ---------------------------------------------------------------------------
# Crest near (from nd terrain.py)
# ---------------------------------------------------------------------------
def crest_near(grid, dam_xy, top_m, dam_height_m, radius_m=400.0):
    """The dam point may be a label beside the embankment.  The highest ground
    within radius_m is taken as the crest ONLY when it is within
    max(3 ft, 25 % of the dam height) of the known top of dam, so a hill or a
    road is not mistaken for the dam.

    Returns ((x, y), note) or (None, reason).
    """
    xs = grid.x_centers()
    ys = grid.y_centers()
    X, Y = np.meshgrid(xs, ys)
    z = grid.masked()
    d = np.hypot(X - dam_xy[0], Y - dam_xy[1])
    near = (d <= radius_m) & np.isfinite(z)
    if not near.any():
        return None, "no DEM within {:.0f} m of the dam point".format(radius_m)
    r, c = np.unravel_index(np.argmax(np.where(near, z, -np.inf)), z.shape)
    tol = max(3.0 * FT_M, 0.25 * dam_height_m)
    if abs(z[r, c] - top_m) > tol:
        return None, ("the highest ground within {:.0f} m ({:.1f} ft) is not the top of dam "
                      "({:.1f} ft)".format(radius_m, z[r, c] / FT_M, top_m / FT_M))
    return (float(X[r, c]), float(Y[r, c])), (
        "dam point moved {:.0f} m to the highest ground nearby ({:.1f} ft, the top of dam is "
        "{:.1f} ft)".format(d[r, c], z[r, c] / FT_M, top_m / FT_M))
