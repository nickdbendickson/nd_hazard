"""Dam-inventory standardisation: units, status/size filters, shared-volume flags.

Pure Python so it can be tested on the real DWR dBase file outside QGIS.
DWR convention: 0 means "not recorded" for height, storage, area and length.
"""
FT_M, AF_M3, AC_M2 = 0.3048, 1233.4818375, 4046.8564224

# ND DWR "Dams by Hazard Class" export (10-character field names)
DWR_DEFAULTS = {"ID": "structure_", "NAME": "dam_name", "H": "max_dam_he", "NS": "normal_vol",
                "MS": "max_pool_v", "SA": "normal_are", "HZ": "hazard_cla",
                "STATUS": "status", "NIDREF": "national_i", "LEN": "dam_length"}

# 0.19.0: the three DWR inventories. Their structure numbers are SEPARATE number series (unknown
# structure 2 is not dam 2), so each kind gets its own dam_id prefix and only real dams join the
# dam lines by structure number.
KIND_DAM, KIND_UNKNOWN, KIND_DUGOUT = "dam", "unknown structure", "dugout"
KINDS = (KIND_DAM, KIND_UNKNOWN, KIND_DUGOUT)
ID_PREFIX = {KIND_DAM: "DWR-", KIND_UNKNOWN: "UNK-", KIND_DUGOUT: "DUG-"}
# what "auto" keeps: unknown structures are almost all status "Unknown" or blank (2,487 and 1,140
# of 3,767 in the 2026 export, 10 "Intact"); dugouts "Pending" are built but not yet verified
DEFAULT_KEEP = {KIND_DAM: "Intact", KIND_UNKNOWN: "Intact, Unknown, (blank)",
                KIND_DUGOUT: "Intact, Pending"}
# field names in the exports: the 10-character dBase names, the full GeoPackage / service names,
# and the dugout table's own names. The first one present in the layer is used.
FIELD_CANDIDATES = {
    "ID": ("structure_", "structure_id"),
    "NAME": ("dam_name", "name"),
    "H": ("max_dam_he", "max_dam_height"),
    "NS": ("normal_vol", "service_level_storage"),
    "MS": ("max_pool_v", "max_pool_vol", "max_storage"),
    "SA": ("normal_are", "normal_area", "service_level_surface_area"),
    "HZ": ("hazard_cla", "hazard_class", "fed_hazard_class"),
    "STATUS": ("status", "structure_status"),
    "NIDREF": ("national_i", "national_id"),
    "LEN": ("dam_length",),
}
# dugout-only extras: water above the natural ground (what a breach can release), and the
# dugout's height / depth (not a dam height: DWR's max_height mixes cut and spoil)
DUGOUT_EXTRA = {"ABOVE": ("storage_above_grade_acft",), "DUGH": ("max_height",),
                "DEPTH": ("max_depth",), "MAXAREA": ("max_surface_area",)}


def detect_kind(field_names, layer_name=""):
    """Which DWR inventory a layer is: by its name first (the exports are called
    DWR_Unknown_Structures / DWR_Dugouts / DWR_Dams), else by fields only one table has."""
    n = (layer_name or "").lower()
    if "dugout" in n:
        return KIND_DUGOUT
    if "unknown" in n:
        return KIND_UNKNOWN
    f = {x.lower() for x in field_names}
    if "max_storage" in f and ("depth_of_cut" in f or "structure_status" in f):
        return KIND_DUGOUT
    if "rev_structure_id" in f or ("fed_hazard_class" in f and "max_dam_height" in f
                                   and "hazard_class" not in f):
        return KIND_UNKNOWN
    return KIND_DAM


def resolve_fields(chosen, field_names, kind=KIND_DAM):
    """{role: field name or None}. A chosen field that is not in the layer (e.g. the dBase default
    'structure_' on the GeoPackage export, whose field is 'structure_id') is replaced by the first
    candidate that is; returns (fields, [notes])."""
    names = list(field_names)
    lower = {x.lower(): x for x in names}
    out, notes = {}, []
    for role in FIELD_CANDIDATES:
        c = chosen.get(role)
        if c and c in names:
            out[role] = c
            continue
        if kind == KIND_DUGOUT and role == "LEN":
            out[role] = None                 # a dugout's length is the pond, not a dam crest
            continue
        hit = next((lower[x] for x in FIELD_CANDIDATES[role] if x in lower), None)
        out[role] = hit
        if c and hit:
            notes.append("{} field '{}' not in the layer: '{}' used".format(role, c, hit))
    if kind == KIND_DUGOUT:
        for role, cands in DUGOUT_EXTRA.items():
            out[role] = next((lower[x] for x in cands if x in lower), None)
        out["H"] = None                      # see DUGOUT_EXTRA: no dam height for a dugout
    return out, notes


def keep_for(text, kind):
    """The status filter: "auto" (the default) = DEFAULT_KEEP of the layer's kind."""
    t = (text or "").strip()
    return DEFAULT_KEEP[kind] if t.lower() == "auto" else t


def _txt(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _num(v, mult):
    try:
        if v is None or v == "":
            return None
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x == 0 else x * mult


def standardize(get, fld, metric=False, tag="DWR", idx=0, kind=KIND_DAM):
    """get(field_name) -> raw value.  fld maps role -> field name (or None).

    0.19.0 kind: a dugout's storage for the breach is the water ABOVE the natural ground
    (DWR storage_above_grade_acft) when recorded; its total storage (mostly below the ground)
    is kept as "total" for the report and never used as the breach volume."""
    h_m, v_m, a_m = (1.0, 1.0, 1.0) if metric else (FT_M, AF_M3, AC_M2)
    g = lambda role: get(fld[role]) if fld.get(role) else None
    rec = {"id": _txt(g("ID")) or f"{tag}{idx}", "name": _txt(g("NAME")),
           "h": _num(g("H"), h_m), "ns": _num(g("NS"), v_m), "ms": _num(g("MS"), v_m),
           "sa": _num(g("SA"), a_m), "hz": _txt(g("HZ")), "status": _txt(g("STATUS")),
           "nidref": _txt(g("NIDREF")).upper(), "len": _num(g("LEN"), h_m), "kind": kind,
           "total": None, "above": None, "dug_h": None, "depth": None}
    if kind == KIND_DUGOUT:
        rec["total"] = rec["ms"]
        rec["above"] = _num(g("ABOVE"), v_m)
        rec["dug_h"] = _num(g("DUGH"), h_m)
        rec["depth"] = _num(g("DEPTH"), h_m)
        rec["ns"] = None
        rec["ms"] = rec["above"]             # None when DWR has no above-grade storage
        if rec["sa"] is None:
            rec["sa"] = _num(g("MAXAREA"), a_m)
    return rec


BLANK = "(blank)"


def parse_status_list(text):
    """Comma-separated statuses -> lower-case set; "(blank)" keeps records with no status."""
    return {s.strip().lower() for s in (text or "").split(",") if s.strip()}


def status_ok(status, keep):
    """keep = set of lower-case statuses; empty set keeps everything."""
    st = (status or "").strip().lower()
    if not keep:
        return True
    if not st:
        return BLANK in keep or "blank" in keep
    # DWR cuts long statuses ("Location Inconclusiv", "Location Incon"): a prefix of 12+ letters counts
    return st in keep or any(len(st) >= 12 and k.startswith(st) for k in keep)


def size_skip(h_m, storage_m3, min_h_ft, min_v_af):
    """Skip only when BOTH height and storage are known and BOTH are below the limits.

    An unknown value never causes a skip: a short levee with no recorded storage may
    still hold a lot of water.  Thresholds of 0 disable the filter."""
    if not (min_h_ft or min_v_af) or h_m is None or storage_m3 is None:
        return False
    return h_m < min_h_ft * FT_M and storage_m3 < min_v_af * AF_M3


def shared_volume_flags(vols_m3, min_af=500.0):
    """True where several records carry the identical large volume - typically levee
    or embankment segments of one reservoir, not separate dams."""
    from collections import Counter
    floor = min_af * AF_M3
    cnt = Counter(round(v) for v in vols_m3 if v and v >= floor)
    return [bool(v and v >= floor and cnt[round(v)] > 1) for v in vols_m3]
