"""2.30.0: the usability work (tab order, reasons on a grey Run button, the one-click chain,
the wording before a model run, the result card) and the new functions (what would change it,
the EAP map layout's sources, the owner / county letter drafts, the classification history,
the lifeline detail, the field-check sheet, the suite chain algorithms).
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import json
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests import fake_qgis  # noqa: E402
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402

MODE = fake_qgis.install()
from ND_Dam_Hazard_Helper import preflight as PRE          # noqa: E402
from ND_Dam_Hazard_Helper import resultcard as RC          # noqa: E402
from ND_Dam_Hazard_Helper import whatif as WI              # noqa: E402
from ND_Dam_Hazard_Helper import letters as LT             # noqa: E402
from ND_Dam_Hazard_Helper import lifelineroutes as LR      # noqa: E402
from ND_Dam_Hazard_Helper import fieldcheck as FC          # noqa: E402
from ND_Dam_Hazard_Helper import history as HIS            # noqa: E402
from ND_Dam_Hazard_Helper import autodetect as AD          # noqa: E402
from ND_Dam_Hazard_Helper import chain_algorithms as CA    # noqa: E402


# ------------------------------------------------- 2. reasons on a grey button --
@test
def a_grey_run_button_names_every_missing_input():
    nothing = {"dam_folder": False, "depth": False, "vel": False, "one_d": False}
    rs = PRE.reasons(nothing)
    assert [k for k, _l, _f in rs] == ["dam_folder", "depth", "vel"]
    assert all(fix for _k, _l, fix in rs)                  # every reason says what to do
    tip = PRE.tooltip(rs)
    assert "Depth raster" in tip and "Find rasters" in tip and "3 thing(s)" in tip
    status = PRE.status_text(rs)
    assert status.startswith("Cannot run yet - Dam folder is missing") and "2 more" in status
    # the 1D criterion needs no velocity; the arrival raster is never a reason
    one_d = {"dam_folder": True, "depth": True, "vel": False, "arr": False, "one_d": True}
    assert PRE.reasons(one_d) == []
    assert PRE.tooltip([]) == PRE.READY_TEXT
    # a run in progress is the one reason, and the suite's items are not asked for
    busy = PRE.reasons({"dam_folder": True, "depth": True, "vel": True, "busy": True},
                       blocking=[{"key": "pack", "label": "Offline pack"}])
    assert [k for k, _l, _f in busy] == ["busy"] and "in progress" in PRE.status_text(busy)


@test
def the_suite_set_up_items_come_after_the_windows_own_inputs():
    have = {"dam_folder": False, "depth": True, "vel": True}
    blocking = [{"key": "folder", "group": "folder", "label": "Suite folder", "level": "error",
                 "text": "not chosen", "fix": "Choose a folder in 'Suite data and servers'."}]
    rs = PRE.reasons(have, blocking)
    assert [k for k, _l, _f in rs] == ["dam_folder", "folder"]
    assert "Suite folder (not chosen)" in rs[1][1] and "Suite data" in rs[1][2]
    assert PRE.names(rs) == "Dam folder, Suite folder"
    # a blocking item that is not a dict cannot stop the window
    assert len(PRE.reasons(have, [None, 42])) == 1


# ------------------------------------------------------ 4. the wording before a run --
@test
def before_a_model_run_the_log_says_run_the_breach_model_first():
    empty = {"notes": []}
    assert AD.describe(empty, "") == AD.NO_RASTERS
    text = AD.found_text(empty, "")
    assert "run the breach model first (step 3 of the suite" in text
    assert "recognised" not in text
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "_breach_model"))
    started = AD.no_rasters_text(d)
    assert "_breach_model" in started and "finish or re-run the breach model" in started
    # with rasters the sentence is the old one
    got = AD.found_text({"depth": os.path.join(d, "Depth (Max).tif"), "notes": []}, d)
    assert got.startswith("Rasters found in the dam folder: depth = Depth (Max).tif")


# --------------------------------------------------------- 5. the result card --
def _absaraka_nd():
    """The real Absaraka (DWR 2801) result: ND High only through two BNSF railroad
    stretches at 15 trains/day, 9 structures inundated, 47 losing sole access."""
    return {"cls": "High", "reasons": ["HIGH - 2 inundated rail stretch(es) ..."],
            "judgment": [], "incomplete": [],
            "counts": {"habitable_probable": 0, "habitable_judgment": 0,
                       "habitable_inundated": 9, "lifeline": 0, "roads_high": 0,
                       "roads_medium": 3, "rail": 2, "rail_high": 2, "unverified": 1,
                       "persons_probable": 0, "persons_judgment": 0}}


def _rails():
    return [{"railroad": "BNSF", "subdivision": "KO", "trains": 15.0, "passenger": False,
             "nd_class": "High", "length_ft": 420.0},
            {"railroad": "BNSF", "subdivision": "KO", "trains": 15.0, "passenger": False,
             "nd_class": "High", "length_ft": 180.0}]


@test
def the_card_names_the_one_reason_and_what_would_change_it():
    nd = _absaraka_nd()
    card = RC.build(nd, {"cls": "Significant", "lij": 4.0}, roads=[], rails=_rails(),
                    access={"cut": 47, "structures": 120, "unknown": 0},
                    margin={"lines": ["HIGH rests on ..."]},
                    dam_name="Absaraka Dam", governing_scenario="Main")
    assert card["cls"] == "High"
    assert "2 BNSF railroad stretches, 15 trains/day" in card["reason"]
    assert "the only criterion that makes it High" in card["reason"]
    assert card["would_be_without"] == "Medium"
    assert card["change"].startswith("Medium if the train count on BNSF were under 10 a day")
    assert "confirm the count with the railroad" in card["change"]
    numbers = dict(card["numbers"])
    assert numbers["Habitable structures inundated"] == "9"
    assert numbers["Habitable structures losing their only road access"] == "47 of 120"
    lines = RC.lines(card)
    assert lines[0] == RC.TITLE and "ND hazard class (screening): HIGH" in lines[1]
    assert any("What would change it:" in x for x in lines)
    html = RC.html(card)
    assert "ND HIGH" in html and "What would change it" in html and "&" not in card["reason"]


@test
def the_card_carries_every_flag_that_qualifies_the_class():
    nd = _absaraka_nd()
    nd["provisional"] = True
    nd["provisional_margin_ft"] = 0.7
    nd["incomplete"] = ["structures"]
    card = RC.build(nd, {"cls": "High", "lij": 7.0}, rails=_rails(),
                    access={"cut": 2, "structures": 9, "unknown": 4},
                    flood_cut_off={"reason": "duration cap", "detail": "48 h"},
                    loss={"night": {"par": 20.0, "rates_ok": False},
                          "day": {"par": 11.0}})
    flags = "; ".join(card["flags"])
    assert "flood cut off: duration cap" in flags
    assert "High is provisional" in flags and "0.70 ft" in flags
    assert "structures not evaluated" in flags
    assert "4 structure(s) with no mapped road" in flags
    assert card["provisional"] and "(provisional)" in RC.lines(card)[1]
    # a Low dam's card says there is nothing
    low = RC.build({"cls": "Low", "counts": {}}, {"cls": "Low", "lij": 0.0})
    assert "no habitable structure" in low["reason"] and low["would_be_without"] == "Low"


# ------------------------------------------- 6. "what would change it" explorer --
def _structs():
    return [
        # 0.70 ft below the judgment boundary at an address - the real margin line
        {"id": 7, "habitable": "Yes", "outcome": "Inundated (economic loss)", "max_depth": 1.8,
         "max_vel": 2.1, "margin_high": 2.4, "margin_judg": 0.70, "acer_type": "Dwelling",
         "address": "1234 Main St", "geom": None},
        {"id": 9, "habitable": "Yes", "outcome": "Inundated (economic loss)", "max_depth": 1.0,
         "max_vel": 1.0, "margin_high": 5.0, "margin_judg": 3.2, "acer_type": "Dwelling",
         "address": "22 Elm St", "geom": None},
        # past the boundary: it carries a High
        {"id": 11, "habitable": "Yes", "outcome": "Probable loss of life", "max_depth": 6.0,
         "max_vel": 4.0, "margin_high": -1.2, "margin_judg": -3.0, "acer_type": "Dwelling",
         "address": "9 Oak Ave", "geom": None},
        {"id": 12, "habitable": "No", "outcome": "Inundated (economic loss)", "max_depth": 4.0,
         "max_vel": 1.0, "margin_high": 0.1, "margin_judg": 0.05, "geom": None},
    ]


@test
def the_five_items_nearest_a_boundary_say_what_would_flip_each_one():
    rows = WI.candidates(_structs(), roads=[
        {"id": 1, "name": "Co Rd 14", "aadt": 1800.0, "nd_class": "Medium",
         "road_class": "Local/County", "length_ft": 300.0, "geom": None},
        {"id": 2, "name": "unnamed", "aadt": None, "nd_class": "Low",
         "road_class": "Local/County", "length_ft": 90.0, "geom": None}],
        rails=[{"id": 3, "railroad": "BNSF", "subdivision": "KO", "trains": 8.0,
                "passenger": False, "nd_class": "Medium", "geom": None}],
        nd=_absaraka_nd())
    up = [r for r in rows if r["direction"] == WI.UP]
    down = [r for r in rows if r["direction"] == WI.DOWN]
    assert up and down
    first = up[0]
    assert first["kind"] == "structure" and "1234 Main St" in first["label"]
    assert "depth +0.70 ft" in first["change"] and first["to_class"] == "High"
    rail = [r for r in up if r["kind"] == "rail"][0]
    assert "+2 trains/day" in rail["change"] and rail["to_class"] == "High"
    road = [r for r in up if r["kind"] == "road" and r["margin"] is not None][0]
    assert "AADT +201" in road["change"] and road["to_class"] == "High"
    unknown = [r for r in up if r["kind"] == "road" and r["margin"] is None][0]
    assert "ask the DOT" in unknown["change"]
    # not habitable structures are left out, and every lowering row is marked as such
    assert not any("structure 12" in r["label"] for r in rows)
    carry = [r for r in down if r["kind"] == "structure"][0]
    # the judgment zone counts as probable loss of life, so leaving HIGH means getting below
    # the JUDGMENT boundary (margin_judg), not just out of the high-danger zone
    assert "depth -3.00 ft" in carry["change"] and carry["to_class"] == "Medium"
    table = WI.table_rows(rows)
    assert table[0][4] == "would raise the class"
    assert any(row[4] == "would lower the class" for row in table)
    text = " ".join(WI.lines(rows))
    assert "never applied by the plugin" in text
    # the five nearest, not everything
    assert len([r for r in WI.candidates(_structs() * 4, limit=5)
                if r["direction"] == WI.UP]) == 5


@test
def an_interstate_and_a_passenger_line_have_no_lowering_row():
    rows = WI.candidates([], roads=[
        {"id": 1, "name": "I-94", "aadt": 12000.0, "nd_class": "High",
         "road_class": "Interstate", "geom": None},
        {"id": 2, "name": "US 81", "aadt": 900.0, "nd_class": "Medium",
         "road_class": "US Highway", "geom": None}],
        rails=[{"id": 3, "railroad": "BNSF", "trains": 2.0, "passenger": True,
                "nd_class": "High", "geom": None}])
    assert rows == [] or all(r["kind"] == "road" and r["to_class"] == "High" for r in rows)


# -------------------------------------------- 7. the EAP map layout's sources --
@test
def the_eap_layout_is_filled_from_the_dam_folder():
    from ND_Dam_Hazard_Helper import eap as EAP
    dam = tempfile.mkdtemp()
    out = os.path.join(dam, "outputs")
    for sub in ("EAP", "Classification", "QA"):
        os.makedirs(os.path.join(out, sub))
    for rel in (("EAP", "EAP_Arrival_Bands.tif"), ("EAP", "EAP_Notification_List.gpkg"),
                ("Classification", "Habitable_Structures.gpkg"),
                ("Classification", "Roads_Inundated.gpkg")):
        with open(os.path.join(out, *rel), "wb") as fh:
            fh.write(b"")
    with open(os.path.join(out, "QA", "Data_Vintage.csv"), "w", encoding="utf-8") as fh:
        fh.write("Source,Detail,Date,Note\nNSI,structures,2022-01-01,pack\n")
    with open(os.path.join(out, "Hazard_Classification_Summary.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("x\nND HAZARD CLASSIFICATION (screening): HIGH  |  incomplete: x\n")
    src = EAP.layout_sources(dam, out)
    assert set(("bands", "notifications", "structures", "roads")) <= set(src["layers"])
    assert "rail" in src["missing"] and src["class"] == "HIGH"
    assert src["vintage"] == ["NSI - structures - 2022-01-01"]
    assert EAP.layout_ready(src)[0] is True
    title, sub, vintage = EAP.layout_title_lines("Absaraka Dam", "High", "Main",
                                                 when="2026-10-13", version="2.30.0",
                                                 vintage=src["vintage"])
    assert title.startswith("Absaraka Dam - dam-break inundation")
    assert "ND hazard class (screening): HIGH" in sub and "prepared 2026-10-13" in sub
    assert "NSI - structures - 2022-01-01" in vintage and "Arrival times are hours" in vintage
    # nothing to draw: the sheet says what to run first instead of failing
    ok, why = EAP.layout_ready(EAP.layout_sources(tempfile.mkdtemp()))
    assert not ok and "run the breach model" in why


# ------------------------------------------ 8. the owner / county letter drafts --
@test
def the_letter_drafts_are_factual_and_clearly_drafts():
    res = {"nd": _absaraka_nd(), "tm11": {"cls": "Significant", "lij": 4.0},
           "governing": "Main", "dam_name": "Absaraka Dam",
           "access": {"cut": 47, "structures": 120}}
    card = RC.build(res["nd"], res["tm11"], rails=_rails(), access=res["access"],
                    dam_name="Absaraka Dam")
    facts = LT.facts_from(res, card=card, dam_name="Absaraka Dam", nid_id="ND01234",
                          version="2.30.0", when="October 13, 2026")
    assert facts["structures"] == 9 and facts["access_cut"] == 47 and facts["cls"] == "High"
    owner = LT.owner_letter(facts)
    text = LT.to_text(owner)
    assert LT.DRAFT_MARK in text and "Absaraka Dam" in text and "ND01234" in text
    assert "15 trains/day" in text and "47 habitable structure(s) lose their only road" in text
    assert "[The engineer states here" in text          # no invented duty / notice
    county = LT.county_letter(facts)
    ctext = LT.to_text(county)
    assert "county" in ctext.lower() and "not a warning of a current condition" in ctext
    assert "[placeholder" not in ctext
    left = LT.placeholders(owner)
    assert "[Dam owner name]" in left and "[Name, P.E.]" in left
    # the editable text round-trips through the exporters
    again = LT.from_text(text)
    assert LT.to_text(again).split() == text.split()
    md = LT.to_markdown(LT.OWNER_TITLE, owner)
    assert md.startswith("# " + LT.OWNER_TITLE) and "## Why the class is High" in md


@test
def a_draft_exports_as_a_readable_docx():
    facts = LT.facts_from({}, dam_name="Test Dam")
    paras = LT.county_letter(facts)
    d = tempfile.mkdtemp()
    path = LT.write_docx(os.path.join(d, "draft.docx"), LT.COUNTY_TITLE, paras)
    assert zipfile.is_zipfile(path)
    with zipfile.ZipFile(path) as z:
        assert set(z.namelist()) == {"[Content_Types].xml", "_rels/.rels",
                                     "word/document.xml"}
        xml = z.read("word/document.xml").decode("utf-8")
    assert xml.startswith("<?xml") and "<w:body>" in xml and LT.COUNTY_TITLE in xml
    assert "&amp;" in xml or "&" not in xml           # every & is escaped
    assert LT.write_draft(d, "owner", facts, "md").endswith("Dam_Owner_Letter_Draft.md")


# ------------------------------------------------- 9. the classification history --
@test
def every_classification_run_is_kept_with_its_vintage_and_version():
    dam = tempfile.mkdtemp()
    assert HIS.read_entries(dam) == []
    first = HIS.make_entry({"cls": "Medium", "counts": {"habitable_inundated": 4}},
                           {"cls": "Low", "lij": 0.0}, dam_name="Absaraka Dam",
                           governing="Main", plugin_version="2.29.1",
                           vintage=["NSI - 2022-01-01"], cfg={"one_d": False},
                           when="2026-03-01 09:00")
    HIS.append_entry(dam, first)
    second = HIS.make_entry(_absaraka_nd(), {"cls": "Significant", "lij": 4.0},
                            dam_name="Absaraka Dam", governing="Main",
                            plugin_version="2.30.0", vintage=["NSI - 2024-01-01"],
                            when="2026-09-12 14:00")
    path = HIS.append_entry(dam, second)
    assert os.path.basename(path) == HIS.HISTORY_FILE
    runs = HIS.read_entries(dam)
    assert [r["nd"] for r in runs] == ["Medium", "High"]
    assert runs[1]["plugin_version"] == "2.30.0" and runs[1]["vintage"] == ["NSI - 2024-01-01"]
    assert HIS.class_changes(runs) == [("2026-09-12 14:00", "Medium", "High")]
    one = HIS.history_one_line(runs)
    assert "2 classification run(s) since 2026-03-01" in one
    assert "last change Medium -> High on 2026-09-12" in one
    lines = HIS.history_lines(runs)
    assert any("plugin 2.29.1" in x for x in lines)
    assert any(x.startswith("CLASS CHANGED on 2026-09-12") for x in lines)
    rows = HIS.history_rows(runs)
    assert rows[1][1] == "High" and rows[1][5] == "2.30.0"
    csv_path = HIS.write_history_csv(os.path.join(dam, "h.csv"), runs)
    with open(csv_path, encoding="utf-8-sig") as fh:
        assert "Plugin version" in fh.readline()
    # an unreadable log is not an error: the run goes on and the file is replaced
    with open(os.path.join(dam, HIS.HISTORY_FILE), "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    assert HIS.read_entries(dam) == []
    assert HIS.history_one_line([]) == ""
    assert HIS.append_entry(os.path.join(dam, "no-such-folder"), second) == ""


# --------------------------------------------------------- 10. lifeline detail --
def _grid_graph():
    """Four nodes in a line (0-1-2-3) plus a long way round 1-4-3; the edge 1-2 is flooded.
            0 --- 1 --- 2 --- 3(exit)
                   \\__ 4 __/  (longer)
    """
    edges = [(0, 1, 500.0, 1.0, False), (1, 2, 500.0, 1.0, True), (2, 3, 500.0, 1.0, False),
             (1, 4, 2000.0, 4.0, False), (4, 3, 2000.0, 4.0, False)]
    return {"n_nodes": 5, "edges": edges, "exits": [3]}


@test
def each_lifeline_gets_its_alternative_route_and_a_second_facility():
    graph = _grid_graph()
    facs = [{"name": "Fire 1", "category": "Fire station", "node": 0, "flooded": False,
             "cut_off": True},
            {"name": "Fire 2", "category": "Fire station", "node": 3, "flooded": False},
            {"name": "WWTP", "category": "Wastewater treatment", "node": 2, "flooded": True}]
    rows = LR.detail(graph, facs)
    by = {r["name"]: r for r in rows}
    assert set(by) == {"Fire 1", "WWTP"}                 # Fire 2 is not lost or isolated
    fire = by["Fire 1"]
    assert fire["normal_ft"] == 1500.0 and fire["alt_ft"] == 4500.0
    assert fire["detour_ft"] == 3000.0 and fire["alt_min"] == 9.0
    assert fire["second"] == "Fire 2" and fire["second_min"] == 9.0 and fire["second_within"]
    assert "longer route" in LR.row_line(fire) and "within 15 min" in LR.row_line(fire)
    wwtp = by["WWTP"]
    assert wwtp["status"] == "inundated" and wwtp["second"] == ""
    assert "no second facility of the same kind" in LR.row_line(wwtp)
    text = " ".join(LR.lines(rows))
    assert "no traffic, no turn or dispatch delay" in text and "screening value" in text
    head_rows = LR.table_rows(rows)
    assert head_rows[0][-1] in ("Yes", "No") and len(head_rows[0]) == len(LR.HEAD)


@test
def a_facility_with_no_dry_route_is_reported_as_isolated():
    graph = {"n_nodes": 3, "edges": [(0, 1, 400.0, 1.0, True), (1, 2, 400.0, 1.0, False)],
             "exits": [2]}
    rows = LR.detail(graph, [{"name": "EOC", "category": "Emergency operations center",
                              "node": 0, "flooded": False}])
    assert len(rows) == 1 and rows[0]["isolated"] and rows[0]["alt_ft"] is None
    assert rows[0]["status"] == "isolated - no dry route out"
    assert "no route out avoids the flooded roads" in LR.row_line(rows[0])
    # a second facility too far away is reported as NOT within 15 minutes
    far = {"n_nodes": 4, "edges": [(0, 1, 400.0, 1.0, False), (1, 2, 400.0, 1.0, False),
                                   (2, 3, 60000.0, 40.0, False)], "exits": [2]}
    rows = LR.detail(far, [{"name": "Fire A", "category": "Fire station", "node": 0,
                            "flooded": True},
                           {"name": "Fire B", "category": "Fire station", "node": 3}])
    a = [r for r in rows if r["name"] == "Fire A"][0]
    assert a["second"] == "Fire B" and a["second_within"] is False
    assert "NOT within 15 min" in LR.row_line(a)


# --------------------------------------------------------- 11. field-check sheet --
def _fc_structs():
    return [
        {"id": 3, "habitable": "Unverified", "zone": 1, "outcome": "Probable loss of life",
         "max_depth": 5.5, "max_vel": 3.25, "persons": 2, "acer_type": "House on foundation",
         "occupancy": "RES1", "sources": ({"addr_text": "1234 Main St", "nena": True},)},
        {"id": 4, "habitable": "Yes", "zone": 2, "outcome": "Judgment zone", "max_depth": 2.2,
         "max_vel": 1.0, "persons": 3, "acer_type": "Mobile home", "near": "High",
         "sources": ()},
        {"id": 5, "habitable": "Yes", "zone": 3, "outcome": "Inundated (economic loss)",
         "max_depth": 0.6, "max_vel": 0.2, "persons": 2, "sources": ()},
        {"id": 6, "habitable": "No", "zone": 1, "outcome": "Probable loss of life",
         "max_depth": 9.0, "sources": ()},
    ]


@test
def the_field_check_sheet_lists_the_structures_a_visit_has_to_settle():
    rows = FC.sheet_rows(_fc_structs(),
                         address_fn=lambda r: ((r.get("sources") or ({},))[0].get("addr_text",
                                                                                 ""), "", ""),
                         lonlat_fn=lambda r: (-97.5, 47.0))
    assert [r["id"] for r in rows] == [3, 4]              # not the wet-only, not the "No"
    first = rows[0]
    assert first["address"] == "1234 Main St" and first["depth"] == 5.5
    assert "habitability unknown" in first["why"] and "high danger zone" in first["why"]
    assert "borderline" in rows[1]["why"] and "mobile home" in rows[1]["why"]
    page = FC.page_text(first, "Absaraka Dam", 1, 2)
    for want in ("FIELD CHECK - Absaraka Dam", "1234 Main St", "Max depth (ft):",
                 "Habitable?", "[ ] yes", "[ ] no", "photo", "Visited by"):
        assert want in page, want
    whole = FC.sheet_text(rows, "Absaraka Dam", when="2026-10-13")
    assert whole.count("FIELD CHECK - Absaraka Dam") == 2 and "\f" in whole
    assert "lower the hazard class" in whole
    d = tempfile.mkdtemp()
    md = FC.write_sheet(d, "Absaraka Dam", rows)
    csv_path = FC.write_csv(d, rows)
    assert os.path.basename(md) == FC.SHEET_MD
    with open(csv_path, encoding="utf-8-sig") as fh:
        head = fh.readline().strip().split(",")
    assert head[:2] == ["Struct_ID", "Address"] and head[-1] == "Field_Note"
    assert FC.sheet_rows(_fc_structs(), only_flagged=False) != rows


@test
def the_marked_up_sheet_becomes_engineer_review_decisions():
    text = ("Struct_ID,Address,NSI_Type,Habitable_Confirmed,Type_Confirmed,Persons_Observed,"
            "Photo_File,Visited_By,Visited_Date,Field_Note\n"
            "3,1234 Main St,RES1,Yes,mobile home,4,IMG_01.jpg,A. Engineer,2026-10-20,lived in\n"
            "4,,RES1,no,,,,A. Engineer,2026-10-20,empty shed\n"
            "5,,RES1,could not tell,,,,,,\n"
            "6,,RES1,,,,,,,\n")
    rows = FC.parse_rows(text)
    assert [r["id"] for r in rows] == [3, 4, 5]            # the empty row is left out
    assert rows[0]["category"] == FC.CAT_DWELLING and rows[0]["acer_type"] == FC.TYPE_MOBILE
    assert rows[0]["persons"] == 4 and "IMG_01.jpg" in FC.decision_note(rows[0])
    assert rows[1]["category"] == FC.CAT_NOT_HABITABLE and FC.lowers_class(rows[1])
    assert rows[2]["category"] == FC.CAT_UNVERIFIED and not FC.lowers_class(rows[2])
    got = FC.summary(rows)
    assert got == {"rows": 3, "habitable_yes": 1, "habitable_no": 1, "unverified": 1,
                   "types": 1, "persons": 1, "lowering": 1}
    # a copy Excel saved with ";" and another case of the headers reads the same
    semi = text.replace(",", ";").replace("Struct_ID", "struct id")
    assert [r["id"] for r in FC.parse_rows(semi)] == [3, 4, 5]
    d = tempfile.mkdtemp()
    path = os.path.join(d, FC.SHEET_CSV)
    with open(path, "w", encoding="utf-8-sig") as fh:
        fh.write(text)
    assert len(FC.read_csv(path)) == 3

    class _Store(object):                 # review.ReviewStore's one method this uses
        def __init__(self):
            self.saved = []

        def set_decision(self, rec, crs, ctx, category=None, acer_type=None, persons=None,
                         note="", reviewer=None):
            self.saved.append((rec["id"], category, acer_type, persons, note))
    store = _Store()
    done = FC.apply_to_store(store, _fc_structs(), None, None, rows)
    assert done["applied"] == 3 and done["lowering"] == 1 and done["unmatched"] == []
    assert store.saved[0][:4] == (3, FC.CAT_DWELLING, FC.TYPE_MOBILE, 4)
    assert store.saved[1][1] == FC.CAT_NOT_HABITABLE
    # a row for a structure that is not in this run is reported, not applied
    done = FC.apply_to_store(store, [], None, None, rows)
    assert done["applied"] == 0 and len(done["unmatched"]) == 3


# ------------------------------------------------- 12. the suite chain algorithms --
@test
def the_chain_algorithms_pass_the_dam_through_and_name_a_missing_plugin():
    assert CA.parse_parameters("") == {}
    assert CA.parse_parameters('{"DEM": "a.tif", "N": 2}') == {"DEM": "a.tif", "N": 2}
    assert CA.parse_parameters("A=1\nB=2.5\nC=true\nD=path/x\nbad line") == {
        "A": 1, "B": 2.5, "C": True, "D": "path/x"}
    assert CA.parse_parameters("{not json") == {}
    dam = tempfile.mkdtemp()
    assert CA.dam_id_of(dam) == os.path.basename(dam)
    assert CA.dam_id_of(dam, "ND01234") == "ND01234"
    with open(os.path.join(dam, "dam.json"), "w", encoding="utf-8") as fh:
        json.dump({"nid_id": "ND00042"}, fh)
    assert CA.dam_id_of(dam) == "ND00042"
    keys = [k for k, _l, _a, _p in CA.chain_steps()]
    assert keys == ["screen", "breach", "classify"]        # the pack is off by default
    assert [k for k, _l, _a, _p in CA.chain_steps(pack=True)][0] == "pack"
    assert [s[2] for s in CA.STEPS] == ["ndhazard:buildpack", "nddam:triage",
                                        "ndbreach:rundam", "ndhazard:classify"]
    msg = CA.missing_message(CA.STEPS[2])
    assert "ndbreach:rundam" in msg and "ND Dam Breach Runner" in msg and "release set" in msg


@test
def the_provider_registers_the_four_main_steps():
    if MODE != "real":
        return                            # the registry needs a real PyQGIS
    from ND_Dam_Hazard_Helper.processing_provider import Provider
    names = set()
    for cls in Provider.original_classes():
        names.add(cls().name())
    assert {"buildpack", "screendam", "runbreach", "suitechain", "classify"} <= names


# --------------------------------------------- 1. / 3. the window itself (real QGIS) --
@test
def the_window_opens_on_setup_and_run_and_takes_a_hand_off():
    if MODE != "real":
        return                            # Qt widgets need a real PyQGIS
    from qgis.core import QgsApplication
    app = QgsApplication.instance() or QgsApplication([], False)
    from ND_Dam_Hazard_Helper.dialog import NDDamHazardDialog
    dlg = NDDamHazardDialog()
    tabs = [dlg.tab_widget.tabText(i).replace("&&", "&") for i in range(
        dlg.tab_widget.count())]
    assert tabs[0] == "Setup & Run" and tabs[1] == "Data & Output"
    assert tabs.index("Review") < tabs.index("DSS-WISE Lite") < tabs.index("HEC-RAS plan")
    # both run buttons name the missing input, and so does the status line
    for btn in (dlg.run_all_btn, dlg.run_class_btn):
        assert not btn.isEnabled() and "Dam folder" in btn.toolTip()
        assert "Find rasters" in btn.toolTip()
    assert "Cannot run yet" in dlg.status_label.text()
    assert "No classification run yet" in dlg.result_label.text()
    # the hand-off finds the rasters and offers the whole run in one click
    dam = tempfile.mkdtemp()
    for n in ("Depth (Max).tif", "Velocity (Max).tif", "Arrival Time.tif"):
        with open(os.path.join(dam, n), "wb") as fh:
            fh.write(b"")
    assert dlg.handoff(dam, source="the ND Dam Breach Runner", run=False) is False
    assert dlg.handoff_group.isVisibleTo(dlg) and dlg.handoff_run_btn.isEnabled()
    assert "Run everything now" in dlg.handoff_label.text()
    assert os.path.basename(dlg.depth_edit.text()) == "Depth (Max).tif"
    assert dlg.tab_widget.tabText(dlg.tab_widget.currentIndex()).replace(
        "&&", "&") == "Setup & Run"
    assert dlg.run_all_btn.isEnabled() and dlg.run_class_btn.isEnabled()
    assert hasattr(dlg, "refresh")        # the shared suite home's hand-off hook
    assert "every input is ready" in dlg.result_label.text()
