"""Unit conversions for the user-facing values of the ND dam suite (1.4.0).

The plugins compute in metres (the DEMs and EPSG:5070 are metric) and talk to the engineer in
US customary units: distances and depths in feet, areas in acres, volumes in acre-feet, flows in
cubic feet per second. Cell sizes are the one exception - they are a property of the DEM
(3DEP comes as 1 m, 2 m, 1/3 arc-second) and stay in metres, shown with the feet equivalent.

Exact factors (international foot, US survey acre by its definition from the foot):
    1 ft = 0.3048 m            1 acre = 4046.8564224 m²         1 ac-ft = 1233.48183754752 m³
    1 cfs = 0.028316846592 m³/s
"""

FT = 0.3048                               # metres per foot
ACRE_M2 = 4046.8564224                    # square metres per acre
AF_M3 = 1233.48183754752                  # cubic metres per acre-foot
CFS_M3S = 0.028316846592                  # cubic metres per second per cfs
MILE_M = 1609.344


def ft_to_m(ft):
    return float(ft) * FT


def m_to_ft(m):
    return float(m) / FT


def acres_to_m2(ac):
    return float(ac) * ACRE_M2


def m2_to_acres(m2):
    return float(m2) / ACRE_M2


def af_to_m3(af):
    return float(af) * AF_M3


def m3_to_af(m3):
    return float(m3) / AF_M3


def cfs_to_m3s(cfs):
    return float(cfs) * CFS_M3S


def m3s_to_cfs(q):
    return float(q) / CFS_M3S


def cell_label(cell_m):
    """'2 m (6.6 ft)' for a cell size - the one value the engineer sees in metres."""
    try:
        c = float(cell_m)
    except (TypeError, ValueError):
        return str(cell_m)
    txt = ("{:g}".format(c)) + " m"
    return "{} ({:.1f} ft)".format(txt, c / FT)


# unit words as they appear in parameter labels -> (metric unit, factor metric-per-unit)
# used by the settings grid to show what a value means; the tools do their own conversion.
UNIT_FACTORS = {
    "ft": ("m", FT), "feet": ("m", FT), "acres": ("m2", ACRE_M2), "acre": ("m2", ACRE_M2),
    "ac-ft": ("m3", AF_M3), "acre-ft": ("m3", AF_M3), "acre-feet": ("m3", AF_M3),
    "cfs": ("m3/s", CFS_M3S), "ft/s": ("m/s", FT), "miles": ("m", MILE_M),
}
