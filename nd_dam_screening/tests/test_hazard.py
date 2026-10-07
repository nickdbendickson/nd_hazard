import os, sys, tempfile
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
import hazard as hz  # noqa: E402
import nd_hazard as nh  # noqa: E402

# roads (policy 5.2.1.2, through nd_hazard.road_class)
assert hz.road_level(300, "county road") == "Low"
assert hz.road_level(499, "") == "Low" and hz.road_level(500, "") == "Medium"
assert hz.road_level(1500, "State Highway 6") == "Medium"
assert hz.road_level(2500, "county") == "High" and hz.road_level(None, "Interstate 94") == "High"
assert hz.road_level(100, "Interstate 29") == "High"          # interstate beats a low AADT
for t in ("I-94", "I 94", "I94", "IH-29", "I", "i-29"):
    assert hz.road_level(50, t) == "High", t
assert not hz.is_interstate("US Hwy 2") and not hz.is_interstate("Main St")
# a missing, NaN, zero or negative AADT is UNKNOWN (assumed Medium by default), never Low
for bad in (None, float("nan"), 0, -3, "", "n/a"):
    assert hz.road_level(bad, "county road") == "Medium", bad
    assert hz.road_level(bad, "State Highway 6", unknown="Low") == "Low", bad   # no name heuristic
assert hz.road_level(300, "", sole_access=True) == "Medium"                     # sole access
# rail (5.2.1.3)
assert hz.rail_level(3, False) == "Medium" and hz.rail_level(10, False) == "High"
assert hz.rail_level(None, True) == "High" and hz.rail_level(None, False) == "Medium"
assert hz.rail_level(float("nan"), False) == "Medium"
assert hz.rail_passenger("B") and hz.rail_passenger("yes") and hz.rail_passenger("1")
assert not hz.rail_passenger("T") and not hz.rail_passenger("X") and not hz.rail_passenger(None)
assert hz.rail_passenger("T", codes=("A", "B", "C", "T"))
# habitable (5.2.1.1 with 5.3): habitable unless the occupancy names an uninhabited building
for t in ("Heavy", "Temporary Lodging", "Agriculture", "Duplex", "Residential", "Unclassified",
          "Utility and Misc", None, "", "NULL"):
    assert hz.is_habitable(t), t
for t in ("Barn", "Detached Garage", "Grain Bin", "Utility Shed", "Oil well pad", "SILO"):
    assert not hz.is_habitable(t), t
assert not hz.is_habitable("Agriculture", "Barn")               # either occupancy text counts
assert hz.is_habitable("Barn", flag="yes") and not hz.is_habitable("Residential", flag=0)
assert hz.is_habitable("Barn", flag="maybe") is False           # not yes/no: the words decide
assert hz.is_habitable("Barn", uninhabited=("shed",))           # editable word list
assert hz.words("barn; shed , garage") == ("barn", "shed", "garage")
assert hz.is_mobile("Manufactured Home") and hz.is_mobile(None, "RES2") and hz.is_mobile(1)
assert not hz.is_mobile("Single Family Dwelling") and not hz.is_mobile(None)
# existing class
assert hz.normalize_existing("Significant") == "Medium" and hz.normalize_existing("Undetermined") is None
assert hz.normalize_existing("H") == "High"
assert hz.compare_existing("Medium", "Significant") == "agree"
assert hz.compare_existing("High", "Low") == "higher than existing"
assert hz.compare_existing("Low", "High") == "lower than existing"
assert hz.compare_existing("Low", "Undetermined") == "existing class undetermined"
assert hz.compare_existing("NoData", "Low") == "n/a"
assert hz.DEFAULT_WET_M == 0.1 * 0.3048 and hz.MIN_WET_M < nh.MIN_WET_FT * 0.3048
print("OK")

# ---- comparison-only criteria (FEMA / Feinberg, user curve) ----------------------------
FT = hz.FT
assert not hz.structure_lethal(1.0, 0.5, criterion="feinberg")               # 3.3 ft, DV 5 ft2/s
assert hz.structure_lethal(1.25, 0.1, criterion="feinberg")                  # >= 4 ft
assert hz.structure_lethal(0.8, 9.0, criterion="feinberg")                   # DV 7.2 m2/s > 75 ft2/s
assert hz.structure_lethal(0.8, 1.0, criterion="feinberg", dv_m2s=7.0)       # DV given explicitly
assert hz.structure_lethal(0.62, 0.1, criterion="feinberg", mobile=True)     # mobile: >= 2 ft
assert not hz.structure_lethal(0.62, 0.1, criterion="feinberg")
try:
    hz.structure_lethal(1.0, 1.0, criterion="curve"); raise SystemExit("curve without file accepted")
except ValueError:
    pass
d = tempfile.mkdtemp(); p = os.path.join(d, "c.csv")
open(p, "w").write("velocity_ms,depth_m\n0,1.5\n2,0.6\n5,0.2\n")
c = hz.load_curve(p)
assert hz.structure_lethal(0.5, 3.0, c) and not hz.structure_lethal(0.5, 1.0, c)
assert not hz.structure_lethal(0.15, 8.0, c) and hz.structure_lethal(0.25, 9.0, c)
print("OK (criteria)")
