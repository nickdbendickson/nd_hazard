"""Unit conversions. The plugin takes and writes US customary units (ND DWR
practice); the models run in SI."""

FT = 0.3048                    # m per international foot
US_FT = 1200.0 / 3937.0        # m per US survey foot
MILE = 1609.344                # m
ACRE = 4046.8564224            # m2
ACFT = 1233.48183754752        # m3 per acre-foot
CFS = 0.028316846592           # m3/s per ft3/s
G = 9.80665                    # m/s2

# Default arrival depth (ft): arrival time is when the depth first exceeds this, and it is
# also the 'flooded' depth of the run summary. 0.1 ft is the ND hazard wet threshold, so every
# wet structure gets an arrival time (REG_05.2026 5.4(c)); it was 1 ft before Runner 3.26.3.
# Same value as DWR_Plugins Runner 3.10.53 (#131, numbered 3.10.45 on its branch).
ARRIVAL_DEPTH_FT = 0.1


def ft_to_m(v):
    return None if v is None else float(v) * FT


def m_to_ft(v):
    return None if v is None else float(v) / FT


def acft_to_m3(v):
    return None if v is None else float(v) * ACFT


def m3_to_acft(v):
    return None if v is None else float(v) / ACFT


def ac_to_m2(v):
    return None if v is None else float(v) * ACRE


def cms_to_cfs(v):
    return None if v is None else float(v) / CFS


def cfs_to_cms(v):
    return None if v is None else float(v) * CFS
