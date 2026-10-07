"""Which settings tab (family) each Screening tool's parameters belong to (0.13.0), and the
plain step names the Simple tab shows. Pure Python."""

PLUGIN = "ndscreen"

# tool (algorithm name) -> (tab, order inside the tab)
FAMILIES = {
    "datapackage": ("Dams & data", 0),
    "preparedams": ("Dams & data", 1),
    "inventorydiff": ("Dams & data", 2),
    "terraincache": ("Terrain & storage", 0),
    "estimatestorage": ("Terrain & storage", 1),
    "breachdirection": ("Breach", 0),
    "breachhydrograph": ("Breach", 1),
    "triage": ("Quick screen", 0),
    "runbreachsim": ("Flood simulation", 0),
    "estimateruntime": ("Flood simulation", 1),
    "sensitivity": ("Flood simulation", 2),
    "hazardclassification": ("Hazard", 0),
    "runnerhandoff": ("Hazard", 1),
    "findponds": ("Ponds & dugouts", 0),
    "proposedam": ("Dams & data", 4),
    "reports": ("Reports & checks", 0),
    "comparereference": ("Reports & checks", 1),
    "selftest": ("Reports & checks", 2),
    "preflight": ("Reports & checks", 3),
    "mergeponds": ("Dams & data", 3),
    # runchain: its parameters are the chain's own (work folder, settings) - not in the grid
}
TAB_ORDER = ("Dams & data", "Terrain & storage", "Breach", "Quick screen", "Flood simulation", "Hazard",
             "Ponds & dugouts", "Reports & checks")
NOT_IN_GRID = ("runchain",)

# step key -> plain name (the Simple tab and the chain's messages; the numbers stay in the
# work-folder file names and the Processing tool titles)
PLAIN = {
    "0": "Build the data package",
    "1": "Prepare the dams",
    "2": "Cache the terrain",
    "3": "Estimate storage",
    "4": "Place the breach",
    "5": "Breach hydrograph",
    "6": "Quick screen",
    "7": "Flood simulation",
    "7b": "Detailed runs (Runner)",
    "8": "Hazard classification",
    "9": "Sensitivity",
    "10": "Reports",
    "11": "Compare with a reference model",
    "12": "Estimate the run time",
    "13": "Inventory diff",
    "14": "Self-test",
    "15": "Run the steps (headless)",
    "16": "Find ponds and dugouts",
    "17": "Preflight check",
    "18": "Merge dams and ponds",
    "19": "Propose a dam",
}
# the steps most runs use, in the order the Simple tab lists them; the rest are "Advanced"
MAIN_STEPS = ("0", "1", "2", "3", "4", "16", "19", "18", "5", "6", "7", "7b", "8", "10", "17")
ADVANCED_STEPS = ("9", "11", "12", "13", "14", "15")
# Processing toolbox groups
GROUP_MAIN = ("Screening steps", "steps")
GROUP_ADVANCED = ("Advanced", "advanced")
TOOL_GROUP = {}
for _k in MAIN_STEPS:
    TOOL_GROUP[_k] = GROUP_MAIN
for _k in ADVANCED_STEPS:
    TOOL_GROUP[_k] = GROUP_ADVANCED


def family(tool):
    """(tab, order) of a tool; unknown tools go to the last tab."""
    return FAMILIES.get(tool, (TAB_ORDER[-1], 99))


def plain(step, title=None):
    return PLAIN.get(step, title or step)


def group_for(step):
    return TOOL_GROUP.get(step, GROUP_MAIN)
