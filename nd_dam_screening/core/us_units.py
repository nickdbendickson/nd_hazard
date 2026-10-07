"""0.18.0 US units for what the engineer reads.

The steps hand each other tables and rasters in metres (the DEMs and EPSG:5070 are metric, and
every step reads the working fields of the one before by name), so those stay. What changes in
0.18.0 is what the engineer sees: every result table gets a US-units twin of each metric field
(height_m -> height_ft, td_qp_m3s -> td_qp_cfs, td_vw_m3 -> td_vw_acft, inv_area_m2 ->
inv_area_ac, vel_ms -> vel_fps, dv_m2s -> dv_ft2s), written next to it and refreshed whenever the
metric value is, the maps and the HTML report show feet / cfs / acres / acre-feet, and the rasters
of step 7 come with feet / ft-per-second companions.

Pure Python (no QGIS), so the rules are tested without it."""
import math

FT = 0.3048                               # metres per foot
ACRE_M2 = 4046.8564224                    # square metres per acre
AF_M3 = 1233.48183754752                  # cubic metres per acre-foot
CFS_M3S = 0.028316846592                  # cubic metres per second per cfs

# (metric suffix, US suffix, metric units per US unit): the first suffix that matches wins
SUFFIX_RULES = (
    ("_m3s", "_cfs", CFS_M3S),
    ("_m2s", "_ft2s", FT * FT),
    ("_m3", "_acft", AF_M3),
    ("_m2", "_ac", ACRE_M2),
    ("_ms", "_fps", FT),
    ("_m", "_ft", FT),
)
# fields whose metric unit does not show in the name (peak flows in m3/s)
EXPLICIT = {
    "qp_f95": ("qp_f95_cfs", CFS_M3S), "qp_tr60": ("qp_tr60_cfs", CFS_M3S),
    "td_qp_f95": ("td_qp_f95_cfs", CFS_M3S), "td_qp_tr60": ("td_qp_tr60_cfs", CFS_M3S),
}
# names that end like a metric unit but are not one
NOT_METRIC = frozenset(("dam_m", "item_m", "geom_m", "name_m", "dem_m", "elem_m", "form_m",
                        "norm_m", "sim_m", "algo_m", "param_m", "stream_m", "atm_ms"))


def twin(name):
    """(US field name, metric units per US unit) for a metric field name, or None."""
    n = str(name)
    if n in EXPLICIT:
        return EXPLICIT[n]
    if n in NOT_METRIC:
        return None
    low = n.lower()
    for suf, us, k in SUFFIX_RULES:
        if low.endswith(suf) and len(low) > len(suf):
            return n[:-len(suf)] + us, k
    return None


def twin_pairs(names):
    """[(metric name, US name, factor)] for the metric fields among `names`, skipping a twin
    that would take the name of another metric field."""
    names = list(names)
    taken = set(names)
    out = []
    for n in names:
        t = twin(n)
        if t is None:
            continue
        tn, k = t
        if tn in taken and twin(tn) is not None:
            continue                    # the 'twin' is itself a metric field of this table
        out.append((n, tn, k))
    return out


def convert(value, factor):
    """A metric value in US units, or None for blanks / NaN / text."""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v / factor


def label_pair(metric_m, kind):
    """'{} ft'-style helper for messages: kind 'm' -> feet, 'm3' -> ac-ft, 'm2' -> ac,
    'm3s' -> cfs, 'ms' -> ft/s. Returns the number in US units and the unit word."""
    table = {"m": (FT, "ft"), "m3": (AF_M3, "ac-ft"), "m2": (ACRE_M2, "ac"),
             "m3s": (CFS_M3S, "cfs"), "ms": (FT, "ft/s"), "m2s": (FT * FT, "ft²/s")}
    k, unit = table[kind]
    v = convert(metric_m, k)
    return v, unit


def say(metric_m, kind="m", digits=0):
    """'1,200 ft' for a metric value of `kind` (for log lines and notes)."""
    v, unit = label_pair(metric_m, kind)
    if v is None:
        return "-"
    return "{:,.{d}f} {}".format(v, unit, d=digits)
