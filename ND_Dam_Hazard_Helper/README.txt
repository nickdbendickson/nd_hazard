ND Dam Hazard Classification Helper - Version 2.33.0
====================================================

NEW IN 2.30.0
- What this means, in one screen. The Review tab and page 1 of the PDF report open with a plain
  card: the hazard class, the ONE criterion that carries it, what would change it (the margin),
  the flags that qualify it (flood cut off, provisional, no signed-off fatality-rate table) and
  the numbers underneath. Written for a dam owner and a county emergency manager.
- "What would change it": the five structures, road or rail stretches nearest a class boundary,
  each with the depth, velocity, AADT or train-count change that would flip it and the class it
  would become. Rows that would LOWER a class are marked as such - the plugin never applies them.
- EAP map layout (PDF): Hazard Classification > "EAP map layout (PDF)" fills a print layout from
  the dam folder (title block, dam, inundation shaded by arrival time, structures, roads, legend,
  data vintages) and exports PDF plus GeoPDF where GDAL can write one.
- Letter / notification drafts: a dam-owner letter and a county notification filled from the run,
  editable in the window, saved as .docx or .md. Each one is marked DRAFT - FOR REVIEW, states no
  legal requirement and leaves the signature and regulatory wording to the engineer.
- Classification history: every run appends its summary (class, counts, data vintages, plugin
  version, scenario) to Classification_History.json in the dam folder; "Classification history"
  shows the class over time and what changed with it.
- Lifeline detail: for each lifeline or critical facility inundated or cut off, the alternative
  route length by road and whether a second facility of the same kind is within 15 minutes -
  "isolated" when there is no dry route at all.
- Field-check sheet: one printable page per structure a visit has to settle (address, NSI type,
  depth, velocity, photo slot, habitable yes / no / could not tell), and import of the marked-up
  sheet, which becomes engineer review decisions in the existing review store. A "no" is reported
  as a lowering decision for the engineer to accept; nothing is applied silently.
- Processing algorithms: ndhazard:buildpack, ndhazard:screendam, ndhazard:runbreach,
  ndhazard:classify and ndhazard:suitechain - the four main steps in the Model Designer or
  headless with qgis_process, for a statewide batch.
- Usability: the window opens on "Setup & Run" and the two importers sit after Review; both run
  buttons say why they are grey and name the missing input; a suite hand-off runs "Find rasters"
  at once and offers "Run everything now"; "no rasters recognised" now reads "run the breach model
  first (step 3 of the suite)".
- No hazard class changes in this release: everything above reports, explains or reviews.

NEW IN 2.29.1 (Breach Runner 3.19.0 and Breach Screening 0.27.0 unchanged)
- OpenStreetMap layer files. The offline pack builder now reads OSM_lifelines, OSM_crossings,
  OSM_campgrounds, OSM_trailer_parks and OSM_places from files - the suite data folder's
  osm_layers/ folder (the ND Data Updater builds them from Geofabrik's North Dakota extract), the
  "DWR / HIFLD files" folder, or --files - and goes to the Overpass API (which often times out)
  only for the layers without a file.
- Data & Output > folder search recognises two more layers, mobile-home / trailer parks and
  cities / towns / villages (travel-time table), by name or fields (OSM_trailer_parks, OSM_places);
  a layer picked there is used instead of the download.

NEW IN 2.29.0 (with the ND Dam Breach Runner 3.19.0 and ND Dam Breach Screening 0.27.0, damsuite 1.15.0)
- One button builds the whole offline pack. Plugins > ND Dam Suite > Suite data and servers >
  "Build / update the offline pack..." (or python build_nd_pack.py --out ND_Dam_Suite_Pack) makes
  every layer the plugins need, in one run:
    web layers   buildings (NSI, FEMA USA Structures, address points), roads, rail and crossings,
                 traffic counts, bridges, lifelines, NID dams, USGS 3DHP streams and waterbodies,
                 SARP aquatic barriers, OpenStreetMap, counties
    land cover   NLCD 2021 from the MRLC web coverage service (30 m, EPSG:5070; needs GDAL, so run it
                 inside QGIS)
    files        the DWR exports (dams, dam lines, dugouts, dugout polygons, unknown structures) and the
                 HIFLD layers (fire / EMS stations, emergency operations centers, law enforcement,
                 wastewater plants). They have no public service, so put the files in the suite data
                 folder's dwr/ and hifld/ folders (or pick another folder in "DWR / HIFLD files"; the
                 command line has --files). Files are recognised by name (Dam_Lines.shp, Dugouts_poly.gpkg,
                 Local_Emergency_Operations_Centers_(EOC).geojson ...), reprojected to lon / lat and clipped
                 to the pack's box. The DWR dams come from DWR's public service when there is no file.
    3DHP file    the USGS 3DHP web service often answers HTTP 504 / 502 to a whole-state request. The builder
                 reads it in small tiles, and when the National Map's 3DHP national file (CONUS, FileGDB zip
                 about 12.5 GB, https://apps.nationalmap.gov/downloader/ > Hydrography > 3DHP) is in the
                 suite data folder's 3dhp/ folder (or the "DWR / HIFLD files" folder, or --files), the
                 flowlines and waterbodies are cut out of that file instead - fast, complete and offline.
  A layer that could not be built is listed under NOT IN THE PACK in LAYERS.txt, with what to do. Run
  it again later: layers already in the pack are kept and only what is missing, old or incomplete is built.
- USGS 3D Hydrography Program (3DHP) replaces NHD for streams and waterbodies. Pack layers
  3DHP_flowlines and 3DHP_waterbodies (an older pack's NHD_flowlines / NHD_waterbodies are replaced when
  the pack is updated; the plugins answer to either name, so old packs keep working). Each 3DHP feature
  keeps its own fields (id3dhp, mainstemid, featuretype, streamorder, ...) and gets the NHD ones the tools
  read (gnis_name, gnis_id, ftype, fcode). The servers list has 3DHP as the default and NHD / NHDPlus HR
  as alternatives.
- Aquatic barriers: SARP_dams (about 7,000 dams in and around North Dakota) and SARP_surveyed_crossings
  (field-surveyed road-stream crossings) from the Southeast Aquatic Resources Partnership services behind
  tool.aquaticbarriers.org. The tool's unsurveyed (modelled) road-stream crossings are not in those public
  services and are not in the pack.
- The land-cover request to the MRLC server used the wrong grid axis names and was refused; fixed.

NEW IN 2.28.0 (with the ND Dam Breach Runner and ND Dam Breach Screening released with it)
- Flood cut-off marker: when the Runner's run_report.json next to the flood rasters says the
  model stopped while water was still moving (duration cap, domain-run limit or the largest
  domain side), the summary's classification line ends with "incomplete: flood cut off
  (<reason>)" and the data sources say the inundated extent and the class may be low. The
  class itself is not changed; without a Runner report nothing changes.
- Dam suite home opens a sibling plugin that is installed but not loaded: it is loaded and
  started first, and the status line says so.
- The plugin version in the summary header, the chart sign-off record and the HTTP user agent
  is read from metadata.txt (it was stuck at 2.24.0).
- Export for screening: the waterbodies gap line now says "not in this export" (the pack has
  had waterbodies since 2.17.0).
- tests/ folder: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (no QGIS needed).

NEW IN 2.24.0 (with ND Dam Breach Runner 3.14.0 and ND Dam Breach Screening 0.22.0)

The offline pack is now a folder, ND_Dam_Suite_Pack, with one GeoPackage per layer
(structures/, roads/, railroads/, lifelines/, hydro/, dwr/, hifld/, boundaries/, landcover/).
Each file opens in QGIS by itself. LAYERS.txt in the folder lists every layer, its file,
feature count and source, and what was left out. The DWR inventory files, the HIFLD lifeline
layers and NLCD land cover are inside the pack, so the pack alone is enough. An older
nd_offline_pack.zip still works, and the dwr/, hifld/ and nlcd/ drop-in folders are still read
when the pack lacks a layer.

Data source, shared by the three plugins: Offline pack (choose its folder or zip) or Download
from the servers (the pack is not read). Set it in Suite data and servers in any of the plugins;
the setting is the same in all three.

- Data & Output tab, section 7: Data source - Offline pack or Download, with Folder... and
  Zip... to choose the pack. Build offline pack writes the ND_Dam_Suite_Pack folder.
- With the offline pack, the pack's HIFLD lifeline layers (fire / EMS stations, emergency
  operations centers, law enforcement, wastewater plants) are read with the other lifelines
  when lifelines are on. They win the 300 ft duplicate check against downloaded points.

NEW IN 2.23.1 (with ND Dam Breach Runner 3.13.1 and ND Dam Breach Screening 0.21.1)

No change in this plugin. The shared damsuite (1.9.1) names the new release set; install
the three plugins together.

NEW IN 2.23.0 (with ND Dam Breach Runner 3.13.0 and ND Dam Breach Screening 0.21.0)
- Fast run: a box on the More settings tab (FAST_RUN in the Processing tools, batch too)
  skips the EAP map book and the aerial imagery under the maps for that run. The map book and
  imagery settings are kept; every result is the same.
- No structures in the study area: the message now says which layer has no CRS, or that the
  layers have a CRS and do not overlap the study area (before, it always blamed the CRS).
- Data folder scan: the ND DWR dam inventory (fields structure_id, dam_name, max_dam_height)
  is recognised as dams; DWR unknown-structure and dugout layers are not taken as dams.
- Offline pack: opened strictly read-only (PRAGMA query_only), and a zip entry whose path
  has ".." or a drive is never used (damsuite 1.9.0).

NEW IN 2.22.0 (with ND Dam Breach Runner 3.12.0 and ND Dam Breach Screening 0.20.0)
- Shared suite code only (damsuite 1.8.0): the Runner and Screening keep the computer awake
  while a flood model runs (damsuite/keepawake.py). No change to the hazard classification.

NEW IN 2.21.0 (with ND Dam Breach Runner 3.11.1 and ND Dam Breach Screening 0.19.0)
- Includes all of 2.20.1 below.
- DWR unknown structures and dugouts: when dam.json says the height or storage was ESTIMATED by
  the screening from the DEM (no DWR value), the run log and the summary's data sources say so.
- Suite data lists DWR unknown structures and dugouts found in the dwr/ folder.

NEW IN 2.20.1 (with ND Dam Breach Runner 3.11.0 and ND Dam Breach Screening 0.18.1)
- Fewer non-habitable buildings counted as habitable:
  * FEMA USA Structures occupancy (OCC_CLS / PRIM_OCC) and address (PROP_ADDR) belong to
    the parcel and are copied to every building on it (garage, barn, grain bin). They are
    shown but no longer make a building habitable.
  * On a parcel whose ND 911 address point is on one building, the other buildings are
    outbuildings: one that only the NSI calls residential is Unverified (verify; NSI 2026
    often puts the parcel's RES1 record on a farm shed), one that no source calls habitable
    is Not habitable and listed in QA/Inventory_QA.csv.
  * A 911 address point with no building outline nearby and no NSI structure (field access,
    empty lot, tower, utility site) is Unverified instead of a dwelling.
  * Unverified structures in the flooded area are listed for review with the reason.
- Fixed: on a dam's first run every building outline area was blank (area filters and
  persons by floor area did not apply); a rerun gave the areas.

NEW IN 2.20.0 (with ND Dam Breach Runner 3.11.0 and ND Dam Breach Screening 0.18.0)
- Depth-velocity spreadsheet: after the classification, every flooded habitable structure
  (the report's structures table, governing scenario) is written into a COPY of the hazard
  spreadsheet and plotted on its first tab, on the "Houses on Foundation" and "Mobile Homes"
  charts. Saved as outputs/Classification/<template name>_<dam name>_<YYYY-MM-DD>.xlsx (a
  re-run on the same day replaces that day's copy); the template is never written.
  * Template: templates/Hazard_Spreadsheet.xlsx in the plugin folder, or your own (option
    "Spreadsheet template", Processing DV_TEMPLATE). Its first tab must keep the "Houses on
    Foundation", "Mobile Homes" and "QGIS csv Export" blocks and the two charts.
  * The QGIS csv Export block gets one row per structure (POI = Struct_ID, V ft/s, D ft,
    AT h, V+1, plus the ACER zone and type); each table one row per structure of its type,
    with the template's formulas. More structures than the template's 14 rows: rows are
    inserted and the Mobile Homes block moves down. Every point is labelled with its
    Struct_ID. A depth above the chart raises the depth axis; a velocity above the figure
    (25 / 16 ft/s) is drawn at the right edge, where the classification reads the curves.
  * The template's zoom inset (sample data, 4-6 ft/s) is left out of the copy.
  * Open the copy in Excel: Excel puts the first velocity category at x = 1 (hence V+1);
    LibreOffice draws the houses points 1 ft/s too far right.
- The PDF report has a "Depth-velocity charts" page with the same structures on both
  figures, drawn from the REG_05.2026 table the classification uses (the spreadsheet draws
  its older quartic fits, up to about 0.2 ft different), and a close-up under a chart when
  all its structures fit in a third of the figure. Also saved as
  outputs/Classification/DV_Chart_*.svg.

NEW IN 2.19.0 (with ND Dam Breach Runner 3.11.0 and ND Dam Breach Screening 0.18.0)
- No structure data is "not evaluated", not "no structures". When no building outlines or
  address points were given or downloaded, the structure criteria show "?", the judgment line
  says the class is a minimum, and a LOW result reads "LOW SO FAR ... habitable structures were
  NOT evaluated". Before, it showed 0 structures and "not met".
- The portfolio dashboard reads the Screening summary.csv column height_ft (feet; Screening
  0.18.0 shows US units), and still reads height_m from older reports.

WHAT IT DOES
- One run classifies the dam: every habitable structure (NSI points, ND GIS
  Hub site addresses, FEMA USA Structures outlines, campgrounds) is sampled
  against the Depth, Velocity and Arrival rasters over its outline and 5-ft
  edge ring, roads (AADT) and railroads are checked, lifeline facilities,
  bridges, dams in series and the model extent are screened, and the ND Dam
  Safety Standards REG_05.2026 Sec. 5.2 and USBR ACER TM 11 classes are
  written with a PDF report, spreadsheets and EAP outputs.
- ACER zone rasters (TM 11, USBR 1988) for houses on foundations, mobile
  homes and passenger vehicles from the max depth with the max velocity of
  every cell: 1 = HIGH DANGER (red), 2 = JUDGMENT (yellow), 3 = LOW DANGER
  (green), -9999 = No Data.
- QA layers show exactly what was read: Structure_Evaluation_Areas (the area
  searched around each structure), Structure_Sampled_Cells (the raster cells)
  and Structure_Sample_Points (where each governing value came from).

NEW IN 2.17.0 (with ND Dam Breach Runner 3.9.0 and ND Dam Breach Screening 0.16.0)
- Suite data and servers / Get data for this dam (see SUITE DATA AND SERVERS below).
- Data & Output: data sets you left empty are filled from the dam folder's data/
  "(from dam folder)", only when that data holds the whole study area.
- Export for Screening for one dam folder adds NHD waterbodies, DWR dam lines, DWR
  dams and the DEM path. The offline pack holds NHD waterbodies; build_nd_pack.py runs
  as a script again.
- Another dam folder clears the rasters found for the previous dam and looks again
  (DSS-WISE Lite / HEC-RAS imports in outputs/ are found too); the NID ID box follows
  dam.json; NSI / address tiles are kept a week by default.
  Nothing here was run in a real QGIS.

NEW IN 2.15.0 (with ND Dam Breach Runner 3.7.0 and ND Dam Breach Screening 0.14.0)
- A proposed dam (Screening tool 19) is named in the summary and the PDF.
  The sentence "proposed — embankment inserted from the drawn line" is read
  from dam.json in the output folder or up to four folders above it. A rim
  below the design crest is named with it. Nothing here was run in a real QGIS.

NEW IN 2.14.1 (release set unchanged: ND Dam Breach Runner 3.6.0, ND Dam Breach Screening 0.13.1)
- Runner culverts tool, NDDOT centerline pipes: which end is the inlet. NDDOT's LEFT_ELEV_E /
  RIGHT_ELEV_E are the sides as seen looking from the lower station number to the higher
  (confirmed). The tool now reads that direction at each pipe from its neighbours on the same
  route (the pipe at the next lower measure to the one at the next higher; 22,041 of the
  22,051 located pipes have one), falls back to HWY_DIR (W and S are the opposite carriageways
  of divided highways; their measures still increase east / north), and turns it onto the
  matched road segment. The side the stream comes from is the inlet - the end's elevation is
  put where it physically belongs, whichever is higher. An inlet below the outlet is noted
  ("check the stream line's flow direction and the elevations"); it no longer decides the
  orientation. Only without any direction (no neighbour, blank HWY_DIR, or a route direction
  running across the matched road) is the higher elevation taken as the inlet, and inlet_side
  then says "left (by elevation)" / "right (by elevation)". 2.14.0 used the higher elevation
  always. Checked on the real layer: the neighbour direction agrees with HWY_DIR's cardinal
  direction for 99% of pipes; the other 1% are routes bending locally - the reason to read it
  from the neighbours.

NEW IN 2.14.0 (with ND Dam Breach Runner 3.6.0 and ND Dam Breach Screening 0.13.1)
- Runner culverts tool: NDDOT's two inventories are read, by their field names, so no
  field picking is needed:
  - the NDDOT bridge inventory (Bridge_Inventory_External: BRIDGE_ID, FACILITY,
    FEATURE_INTERSECTED, SERVTYPUNDDESC, DESIGNMAINDESC, TOTAL_SPANS, LENGTH_ENG,
    ROADWIDTH_ENG, CULVERT). Its culvert description ("DOUBLE, 10X7X88' RCB",
    "SINGLE, 10-6X39' SPP", "TRIPLE 66\" X 40' CMPA" ...) is parsed for the barrels,
    span x rise (feet-inches, inches and feet), the barrel length and the shape (RCB = box,
    SPP / CMP = pipe, SPPA / ARCH / CON-SPAN = arch). Of the 1,861 descriptions in the 2026
    export, 1,849 read completely; the rest keep what could be read and say what could
    not. Absurd sizes (a 7,106 ft rise) are dropped with a note, never used. A bridge's span
    is the structure length / spans (average), noted as such.
  - the NDDOT centerline pipes (Center_Line_Pipes: DIAMETER_E, ARCH_E, LENGTH_E,
    LEFT_ELEV_E, RIGHT_ELEV_E, PIPE_DESC, HWY_DIR, REFERENCE_PT, OFFSET_E, LOC_ERROR), the
    second NDDOT layer slot of the tool. Diameter (or arch span x rise; a cattle pass's
    width x height; a size below 12 read as feet, as cattle passes are recorded), barrels
    from the description ("DOUBLE ROW OF ..."), the pipe length and the two end elevations.
  - The end elevations are written as invert_up / invert_dn (feet); 2.14.0 put the HIGHER one
    at the upstream end - 2.14.1 assigns them by the geometry instead (see above). Their
    datum is NOT stated by NDDOT, so the layer alone is not the truth: ND Dam Breach Runner
    3.6.0 compares them with the LiDAR bed before using them (within its tolerance = used as
    given; both ends off alike = a datum shift corrected; otherwise the LiDAR inverts, and
    the run report says so). A Runner before 3.6.0 would use them as given - the suite's
    version check warns about that. PASS_ELEVATIONS off leaves the inverts empty.
  - A line with an inventory barrel length is that long plus the two margins (the barrel
    already runs toe to toe); other lines are placed as before.
  - New information fields: barrel_len_ft, material, inv_text (the inventory text as
    written), elev_left_ft, elev_right_ft, inlet_side, end_section, line_len_src.
  Written against the two geodatabases' field names and values as exported in 2026, not
  against NDDOT documentation; the elevation datum is the thing to verify with NDDOT (the
  left / right convention was confirmed for 2.14.1). Nothing here was run in a real QGIS.

NEW IN 2.13.0 (with ND Dam Breach Runner 3.5.0 and ND Dam Breach Screening 0.13.0)
- "Suite settings" tab: the suite settings file (damsuite_settings.xlsx / .csv, shared with
  the Runner and Screening) as a grid of this plugin's Processing-tool parameters (Value /
  Unit / Starting value, Find, show only YOUR values, Reset / Load / Save). The Processing tools
  open with the file's values; every value can still be changed in a tool's dialog. The
  classification options of the window stay on their tabs and in the settings profiles.
- Runner culverts tool: the four distances are in feet now (SEARCH_FT, ROAD_WIDTH_FT,
  RAIL_WIDTH_FT, MARGIN_FT, marked YOUR value); the metre parameters stay hidden and are still
  read, so a saved model gives the same result.
- Plugins > ND Dam Suite: one menu for the three plugins (each window, Dam suite home, Suite
  settings), next to this plugin's own menu.
Nothing here was run in a real QGIS (test doubles only).

NEW IN 2.12.2 (with ND Dam Breach Runner 3.4.2 and ND Dam Breach Screening 0.11.2)
- Fixes from the FIRST REAL-QGIS RUN (smoke kit, QGIS 3.44.14 LTR on Windows 11):
  - The saved evaluation (fast re-use of the last run) could not be read in
    real QGIS: QgsGeometry.fromWkb was called on the class instead of on a
    geometry. Every run therefore recomputed from scratch; results were right,
    only slower. Same fix in the web export's arrival bands.
  - The shared styles declared their geometry type as a word, which QGIS reads
    as "point": the flood-extent (polygon) and flow-arrow (line) styles were
    refused. Now numeric; all 15 styles load in QGIS 3.44.
  - Mark reviewed: the reviewer's name has no (empty) default any more - QGIS
    shows it as required, as the tool always treated it.

NEW IN 2.12.1 (with ND Dam Breach Runner 3.4.1 and ND Dam Breach Screening 0.11.1)
- Stability release; NOT RUN IN A REAL QGIS (simulated QGIS / Qt only). The
  classification is unchanged: the same inputs give the same class as 2.12.0.
- GDAL behaves as this plugin expects again when the other two plugins are
  installed: they used to switch GDAL's Python exceptions on for the whole QGIS
  session, so a missing or unreadable raster raised an error here instead of
  being skipped. They no longer do (damsuite 1.3.0, gdalsafe).
- The OGR in-memory driver is looked up as "MEM" first (GDAL 3.11 folded the
  old "Memory" driver into it).
- Dam suite home: a damaged dam.json no longer stops the window listing the
  other dams; it is listed with "dam.json cannot be read: ..." (damsuite 1.3.0
  also checks every status / source / conflict entry when a record is read).

NEW IN 2.12.0 (with ND Dam Breach Runner 3.4.0 and ND Dam Breach Screening 0.11.0)
- NOT RUN IN A REAL QGIS (simulated QGIS / Qt only, as every release). The
  classification itself is unchanged; a run with the same inputs gives the
  same class as 2.11.0.
- Windowed raster reading. Every whole-raster pass (the flooded extent, the
  structure sampling, the fast cell selection, the arrival / travel time, the
  coarse wet mask, the rules comparison reader) now reads strips or windows
  of at most READ_BLOCK_TARGET_CELLS (4,000,000) cells, so a county-scale
  raster no longer needs the whole array in memory. Results are identical
  (tests compare cell by cell, with NoData, scale/offset and a misaligned
  velocity grid). Measured on a 4000 x 4000 raster: whole read 258 MB of
  working memory; windowed 41-112 MB depending on the pass.
- "Out of date" by content, not by date. The re-use of the last evaluation
  and of the products (downloads, inventory, rasters) is keyed on a content
  digest of every file (damsuite.chain.file_digest: SHA-256, sampled above
  2 GB - the same digest the Runner's manifest carries) instead of path /
  size / date. A file copied or re-saved without a change is still re-used;
  a raster rewritten with other values but the same size and date is seen
  as changed. QA/Run_Settings.json records the digests ("evaluation").
  Memory layers keep the 2.11 rule.
- Reviewer sign-off: "Mark reviewed..." (Dam suite group) / ndhazard:
  markreviewed writes the "reviewed" status to dam.json (reviewer, decision
  Accepted / Accepted with comments / Returned, comments, open items, the
  SHA-256 of the reviewed package's MANIFEST.csv) and a line to
  outputs/QA/Review_Log.csv; nothing else in dam.json is touched. The
  portfolio dashboard's next action takes "review" from the open items of a
  review that is newer than the classification. The decisions are this
  plugin's words for its record, not a regulatory finding.
- Dam suite home (Plugins > ND Dam Hazard > Dam suite home...): one window,
  the same in all three plugins, listing every dam of a Dams folder with its
  dam.json state and next action and opening the plugin that does it; it
  points this window at the dam folder.
- Layer styles: styles/*.qml (one look for the three plugins: class colours,
  depth / velocity / arrival ramps, ACER zones) and suite_styles.py (applies
  them by file name). This plugin keeps its own programmatic styling; the
  QML files are the same colours for layers you load by hand or from the
  other two plugins.
- Shared package damsuite 1.2.0: Screening's manifests are found by the
  model-chain check; "refresh" next action after an inventory change.

NEW IN 2.11.0 - THE REG_05.2026 CURVE TABLE
- The house (ND Fig. 5.1) and mobile-home (ND Fig. 5.2) danger zone curves are
  now the table the engineer digitized from the REG_05.2026 figures (stated
  accuracy +/-0.06 ft), linear between the tabulated velocities. The same
  table is in ND Dam Breach Runner 3.3.1 and ND Dam Breach Screening 0.10.1
  (damsuite/tm11_curves.py), so the three tools use one set of curves.
- Until 2.10.0 the curves were quartic fits. They differ from the table by up
  to 0.10 ft (house high line at 0-1 ft/s: 5.86 vs 5.97 ft) and 0.18 ft (house
  low line at 18-19 ft/s). A structure within that distance of a line can
  change zone; nothing else changed. A package made with 2.10.0 or earlier
  reproduces as DIFFERENT where that happens.
- Check of the table against screenshots of the two figures (pixel trace, not
  a person's check): houses median 0.03 ft, 95 % within 0.05 ft (high line)
  and 0.09 ft (low line) - the low line's tail differs most (the table is 0 ft
  from 19 ft/s; the figure shows a sliver about 0.06 ft thick to ~20.5 ft/s);
  mobile homes median 0.008 / 0.004 ft, 95 % within 0.02 ft.
- A depth exactly on a line takes the HIGHER zone (ND 5.3), as before.
  Velocities above a figure's range are read at its last value, as before.
- Passenger vehicles (TM 11 Fig. 4, not in REG_05.2026) keep their quartic.
- The run log, QA/Run_Settings and the report sources name the table and its
  SHA-256.
- NOT RUN IN A REAL QGIS (simulated QGIS / Qt only, as all releases).

QUICK START (2.9.0)
Tabs, left to right: DSS-WISE Lite, HEC-RAS plan, Setup & Run, Data & Output,
Hazard Classification, Review, Review: roads & facilities, Readme.
0. (Only when your results come from DSS-WISE Lite or a HEC-RAS plan) DSS-WISE
   Lite tab: import the results package. HEC-RAS plan tab: read a .p##.hdf
   plan and either "Import" rasters or "Use this plan for the classification"
   (converted at run time, kept in outputs/HECRAS_plan_cache).
1. Setup & Run tab: pick the dam folder. The rasters are found by name
   (RAS Mapper "Depth (Max)", "Velocity (Max)", "Arrival Time", "D * V
   (Max)"; DSS-WISE names; *_Hmax / *_Vmax / *_Dmax). Check them and the
   breach model (2D / 1D); "Find rasters" searches again.
2. Data & Output tab: output options and every data set. Optional: "Data
   folder" - browse to a folder on this computer and "Search folder"; the data
   sets of groups 3-6 found there are used FIRST, then QGIS layers, then the
   internet. A layer you pick by hand in a group list always wins. "Download
   data now" saves the rest to outputs/Data.
3. "Check inputs" (optional): units, a water-surface raster given as depth,
   arrival in minutes, missing velocity, coordinate systems, services.
4. "Run everything" (Data & Output tab): data sets to outputs/Data
   (optional), checks and the Hazard Classification in one background task -
   QGIS stays usable, the progress bar's Cancel stops it.
5. Hazard Classification tab: the result panel lists the class, every ND
   criterion and every judgment item - double-click a row to zoom to it.
   Review tab: decide the flagged structures, "Save and re-run" - seconds,
   because the downloads, inventory and sampling of the last run are re-used
   while rasters, layers and their settings are unchanged. "Review: roads &
   facilities" tab: the same for road / rail stretches, bridges / crossings
   and lifeline facilities.

NEW IN 2.10.0 (see CHANGES_2.10.0.txt) - THE DAM SUITE
This plugin, ND Dam Breach Runner 3.2.0 (breach models) and ND Dam Breach Screening 0.9.0
(statewide screening) now work as one system. THIS PLUGIN IS THE CLASSIFICATION; the other
two plugins' classes are previews. NOTHING in 2.10.0 was run in a real QGIS - it was tested
against the plugin's simulated QGIS only. Every threshold below is a screening value YOU
choose (stated default), not a requirement of REG_05.2026 or TM 11.
- dam.json (in the dam folder): one record per dam that all three plugins read and write -
  ID, name, NID, DWR structure number, dam point, height, storage, surface area, top of dam
  and its datum, existing class; which tool set each value; the status (screened /
  modelled / classified / reviewed) with dates; and the values the tools did NOT agree on
  (a tool never overwrites another tool's value - the disagreement is listed). A run takes
  the dam name and NID from it when you leave them empty and records "classified" with the
  class. Group 8 shows the record and the NEXT ACTION (screen, model, classify, review,
  refresh, none). The older dam_location.json is still read.
- Version checks at start: a message when the Runner or Screening is too old for this
  release (e.g. a Runner older than 3.1.1 could hand the breach raster over as its own
  baseline).
- Hazard Classification tab, group 8 "Dam suite" (always shown): "Model gate before every
  classification run" (+ "Stop the run when the gate FAILs") and buttons that open these
  Processing tools with the dam's folders filled in:
  * Model gate (ndhazard:modelgate): flood at the model edge, no-data holes in the wet area,
    the Runner's mass balance and warnings, velocities near the breach, units, the Runner's
    run manifest - PASS / WARN / FAIL with the reason (QA/Model_Gate.csv).
  * Class probability (ndhazard:ensemble): classifies every run of a Runner ensemble
    (Probabilistic tool with "Write each run's rasters") with this dam's settings and data
    and gives P(ND High / Medium / Low), P(TM 11 classes) and per structure the probability
    of the probable-loss-of-life zone (QA/Class_Probability.csv / .html). ONLY AS GOOD AS
    THE ENSEMBLE'S PARAMETER DISTRIBUTIONS (the Runner's defaults are placeholders).
  * Quick creep screen (ndhazard:quickcreep): a NEW structures layer (a new USA Structures
    or NSI release) against every dam's classified flood - new structures in each flood
    area and "re-run recommended". Nothing is re-classified.
  * Rules comparison (ndhazard:rulecompare): re-classifies this run's structures with the
    Runner / Screening rules (their nd_hazard.py) and explains every difference (sampling
    area, habitability, mobile home type, chart / method, judgment zone rule).
  * Chart sign-off (ndhazard:chartsignoff): writes the ACER TM 11 chart file the Runner and
    Screening need (their chart method stops without one) from THIS plugin's curves, and
    overlays the curves on your scans of TM 11 Figs 2 and 3 for checking. The file says
    "digitized" only when two DIFFERENT people are named as digitizer and checker. The tool
    records the names; it cannot confirm that a check was made.
  * Review a package (ndhazard:reviewpackage): the reviewer's view of a submittal package -
    every file against MANIFEST.csv, the model chain, optional reproduction, and a checklist
    (Review_Checklist.csv) the reviewer fills in. Nothing is written into the package. The
    checklist items are this tool's own checks, not the State's review criteria.
  * Culverts for the Runner (ndhazard:runnerculverts): culvert / bridge lines, upstream end
    first, with the Runner's field names, where an NBI / OpenStreetMap / NDDOT structure
    sits at a stream crossing a road or railroad. Sizes, barrels, shape and barrel length
    from NDDOT's bridge inventory and centerline pipes (2.14); the pipes' end elevations
    as inverts, checked by the Runner against the LiDAR. Unknown values stay empty.
  * Export for Screening (ndhazard:suiteexport): one GeoPackage in Screening's tool-0 schema
    from the offline pack or your downloads - structures with this plugin's habitability,
    roads with AADT matched as the classification does, railroads, flowlines, lifelines,
    NID dams. NHD waterbodies, DWR dam lines and DEMs are not in the pack.
- Model chain: every run writes QA/Model_Chain_Check.csv / .txt - are the classified
  rasters exactly the files the ND Dam Breach Runner wrote (SHA-256 against its
  run_manifest.json)? HEC-RAS / DSS-WISE results have no manifest: fingerprints are
  recorded, "chain not checked". The submittal package carries the manifest, the check and
  dam.json.
- Portfolio dashboard: also dam.json, the Screening class / triage / borderline / data gaps,
  the Runner's last model (date, manifest check, class preview) and a Next action column;
  dams needing an action come first.
- Runner ensemble folders (<dam>/Probabilistic) are never taken for the dam's own rasters.

NEW IN 2.9.1
- A run is STOPPED when one raster file is given for two things: a breach's
  no-breach baseline whose depth is the breach depth raster itself (nothing would be
  charged to the dam - a High dam came out Low in a test), or one file as depth and
  as velocity / arrival / D x V. Checked in the dialog, the Processing algorithm and
  the pipeline. A baseline that shares only the breach's velocity or arrival raster
  is run with a warning in the run log.
- Rasters of the DIFFERENCE between two floods ("Incremental Depth (Max)", "... WSE
  Rise", "Difference", "_diff") are never taken for a flood depth by the raster
  search; they are listed in the notes.

HANDING RESULTS TO THIS PLUGIN (other tools, scripts, qgis_process)
- Call ndhazard:classify with DAM_FOLDER only. The rasters are found in that folder
  by name - breach AND no-breach baseline (a file or folder name starting with
  "Baseline", "No breach", "Non-breach", "No failure", "Without breach", "Existing
  conditions" ...). This is how the ND
  Dam Breach Runner hands a dam over from its v3.1.1 (its folder names are read as
  shown below).
- Give DEPTH / VELOCITY / ARRIVAL / DXV / BASE_* only with the right file for each.
  Never fill them by matching words: "No-breach baseline: max depth" and "max depth x
  velocity" both contain "depth".
- Units expected: depth ft, velocity ft/s, arrival hours after breach start
  ("Check inputs" warns when a raster looks like metres or minutes).
- Result: outputs ND_CLASS (the ND REG_05.2026 class - use this one), TM11_CLASS,
  OUTPUT_FOLDER, REPORT_PDF, SUMMARY.
- ND Dam Breach Runner dam folder, as read by this plugin:
    Depth (Max).tif -> breach depth      Baseline Depth (Max).tif -> baseline depth
    Velocity (Max).tif -> breach velocity  Baseline Velocity (Max).tif -> baseline
    Arrival Time.tif -> breach arrival   Baseline Arrival Time.tif -> baseline
    Incremental ... / WSE (Max).tif / _breach_model\*  -> not used

NEW IN 2.9.0 (see CHANGES_2.9.0.txt)
NOTHING in 2.9.0 was run in a real QGIS before release - it was tested against the
plugin's simulated QGIS only. Check the first runs. Every threshold below is a
screening value YOU choose (starting values, not requirements); none is a regulation.
Interface
- Tab order: DSS-WISE Lite, HEC-RAS plan, Setup & Run, Data & Output. "Run
  everything" is on the Data & Output tab.
- Setup & Run: *_Hmax, *_Vmax and *_Dmax rasters (e.g. Depth_Hmax.tif,
  Dam_Vmax.tif) are found; they used to be skipped as a "time step".
- Data & Output, "Data folder": search a folder (and its sub-folders) for the data
  sets of groups 3 (structures), 4 (roads, rail, crossings), 5 (dams, lifelines,
  boundary, parcels) and 6 (channel). Files are recognised by name, geometry and
  fields; you can change every pick in the table; the picks are saved with the
  project. Nothing is written into the folder. Order of use: folder, QGIS layer
  tree (auto-detect), internet. The run log and QA/Run_Settings.json say which.
- HIFLD is no longer downloaded (HIFLD Open closed in Aug 2025; the NASA NCCS copy
  stopped answering). "Get HIFLD (hsdl.org)" opens https://www.hsdl.org/hifld -
  download the layers there into your data folder and search it; fire
  stations, EMS, law enforcement, EOCs, wastewater etc. are recognised by file name
  and fields.
- ND GIS Hub (NENA) address points now carry into the final structure layers:
  Address, City, ZIP, ND_Site_NGUID, ND_Place_Type, ND_Address_Points.
Review
- Click a column heading to sort (again: reversed). New "Vel ft/s" column (maximum
  velocity) next to "Depth ft". "Open in web map" opens the structure's location (Google Maps by
  default; WEBMAP_URL_TEMPLATE in the data-source file changes it).
- ACER type picks up mobile homes from NSI RES2 / bldgtype H, mobile-home words,
  NENA place types, lot / space / trailer unit numbers, OpenStreetMap mobile-home
  parks and (optional) the footprint shape of a single-wide (width, length and
  ratio are your screening values). Type_Basis says which evidence was used.
- New tab "Review: roads & facilities": roads, railroads, bridges / culverts /
  low-water crossings and lifeline facilities with their depth, velocity and
  arrival; decide each one (kept in Facility_Review_Decisions.csv, applied on
  every re-run, in the submittal package and in a reproduction).
Maps
- Aerial imagery under the maps of the PDF report and the EAP map book (USGS
  National Map imagery by default; AERIAL_IMAGERY_URL in the data-source file).
  Offline, or when the service does not answer, the maps are made without it
  and the page says so.
Hazard Classification tab, group 7 "Version 2.9 options" (all optional)
- Fast cell selection (numpy, no GEOS) for the whole-area / ring / band sampling -
  the same cells, compared with the GEOS path in the tests.
- Pyramids (.ovr) for large input rasters (opt-in; writes .ovr next to YOUR
  rasters) and for the ACER zone rasters. Drawing only - values are read at full
  resolution.
- "Estimate run...": what the run will read, download (tiles, requests, cache and
  data folder counted) and write, with a time range. An estimate, not a promise.
- Consequence sensitivity: lives in jeopardy / loss of life with the other time of
  day and the other severity method - QA table only, the class does not change.
- Evacuation routes and road closures as layers (from the evacuation timing
  table; a screening drawing, not an evacuation plan).
- Flood arrival in the QGIS Temporal Controller (arrival contours, notification
  list, road closures appear at their time).
- KMZ (Google Earth) and GeoJSON copies of the EAP layers in outputs/EAP/Web.
- Settings profiles: "Save / Load settings profile...", "Compare profiles..." -
  a firm's or a reviewer's standard settings as a JSON file (dam-specific paths
  are left out); the profile's name and hash go into the run log and package.
- "Reproduce a package...": re-runs a submittal package (made with "include the
  input data") in an empty work folder and reports IDENTICAL / DIFFERENT with the
  reasons. The package now records QA/Run_Settings.json.
- "Portfolio dashboard...": one HTML / CSV page for a folder of dams (class, lives
  in jeopardy, open review items, data vintage, creep and series flags).
- Parcel habitability hints: from YOUR parcel layer and the land-use / improvement
  value fields YOU choose - a hint on the Review tab, never an automatic change.
- "Fill form template...": fills {{KEY}} placeholders of YOUR template (.docx,
  .txt, .md, .html, .csv) from the run's files. The State's form is not built in
  and no form field is guessed; "Save form key list" lists every KEY.
- Offline pack: an R*Tree spatial index makes study-area queries fast (older packs
  still work without it).
Batch and Processing
- Batch classification (ndhazard:batch) and the creep watch: "Workers" > 1 runs
  that many dams at once in qgis_process child processes (one at a time when
  qgis_process is not found).
- New Processing algorithms: ndhazard:portfolio, ndhazard:reproduce,
  ndhazard:fillform (10 algorithms in all).
- A HEC-RAS plan (.p##.hdf) can be used directly as a scenario's input (Setup tab
  or the scenario table), converted with the HEC-RAS plan tab's terrain, breach
  start, arrival depth and cell size.

NEW IN 2.8.0 (see CHANGES_2.8.0.txt)
Hazard Classification tab, group 6 "Extra analyses and speed" (tick "Show advanced
options"). Everything is OFF by default, none of it changes the classification, and
every threshold is a screening value YOU choose (starting values, not requirements).
- Sensitivity table: re-classifies with depth and velocity scaled by 0.8 ... 1.2
  (your factors) from the values already read - QA/Sensitivity.csv (+ the structures
  that change zone) and a verdict line in the summary.
- Downstream termination point: where the breach flood (or its rise over the
  no-breach baseline) stays below your threshold - QA/Termination_Point.csv and the
  layer EAP_Termination_Point; warns when the model ends before the flood does.
- Second population source: a census polygon layer or a population raster you
  supply, compared with the plugin's structure count - QA/Population_Source_*.csv.
- Evacuation timing: latest safe departure against flood arrival along the road
  network (needs the arrival raster and a road layer saved as a file) -
  QA/Evacuation_Timing.csv. A screening table, not an evacuation model.
- Data vintage: QA/Data_Vintage.csv lists every data set with the date it was fetched.
- Download cache: NSI / ArcGIS tiles are kept per tile with their date; later runs
  fetch only tiles older than N days. Off for a normal run (every run reads current
  data); the creep watch turns it on (7 days). "Clear tile cache" deletes it.
- Quick re-run: skips the PDF report and the map book (kept from the last full run).
- Creep watch "Workers": more than 1 classifies that many dams at once in
  qgis_process child processes (falls back to one at a time when qgis_process is not
  found; the plugin must be enabled in that QGIS profile).
- Dams in series... button (and ndhazard:damsinseries): which dam's breach flood
  reaches which other dam - a screening table, not a combined breach.
- Review tab: "Export decisions..." and "Merge from other engineer..." - two engineers
  review the same dam and merge; conflicts are listed with reviewer and date, a backup
  copy of your decisions is made, Review_Merge_Report.csv lists every row.
- Offline pack build: resumes at the chunk that stopped, slows down and recovers
  when a service says 429 / 503 / 504, and shows every wait and retry.

NEW IN 2.7.1 (see CHANGES_2.7.1.txt)
- Hazard Classification tab > Criteria > "Max depth / velocity read from": the
  default still reads every cell in the building outline plus its 5-ft buffer
  (interior cells included). "Buffer ring only" reads just the ring outside the
  wall; "Band" reads 5 ft either side of the wall - the Depth / Velocity Max
  points in Structure_Sample_Points then lie along the buffer / outline.

NEW IN 2.7.0 (see CHANGES_2.7.0.txt)
- Hazard Classification tab, group 5: breach scenarios and the no-breach
  baseline in a table (add, add from a folder, find the baseline in the dam
  folder); the results table shows every scenario side by side.
- Population at risk for night and day; seasonal (camp) shares; non-residential
  damage from depth-damage curves YOU supply (a CSV; no curve values ship).
- DSS-WISE HCOM cross-check (population): the plugin's count against the HCOM
  table, like with like. It does not change the classification.
- Hazard-creep watch: "Creep watch..." button (Hazard Classification tab), or from the command line
  qgis_process run ndhazard:creepwatch --PARENT=<folder of dam folders>.
- "Make submittal package" button: the last run's files in one dated folder with a
  SHA-256 manifest, README and cover-letter template (optional zip).
- Review tab: "Aerial imagery when zooming" (USGS National Map, at the bottom of
  the layer tree; the address is in the data-source file).
- The evaluation is saved with the outputs and re-used after QGIS restarts.

NEW IN 2.6.2 (see CHANGES_2.6.2.txt)
- Both 2.6.1 releases in one: the offline data pack AND the fix for QGIS
  freezing during downloads.
- Offline data pack: a data set with features that could not be read when
  the pack was built is no longer treated as complete; the plugin uses it
  only when the service cannot be reached, with a warning. NID: the USACE
  service and the mirror are tried before an incomplete pack. Check inputs
  no longer unpacks the pack (no freeze). A "#" in the pack's path works.
- Faster: roads, railroads and the sole-access network are read from the
  rasters in a few large reads instead of one per line (same values); the
  ACER zone rasters and EAP arrival maps are re-used when made from the same
  rasters; review decisions are matched through a grid.
- The run log ends with TIMINGS: how long each stage took.

NEW IN 2.6.0
- One run. Main Consolidation and the POI tab are gone; the classification's
  structure inventory is the one consolidation of points and outlines. The
  ACER zone rasters are written by the classification (Classification/
  Hazard_Foundations.tif, Hazard_MobileHomes.tif, Hazard_Vehicles.tif;
  option on the Hazard Classification tab). Points of interest: give them as
  the lifeline facilities layer on the Data & Output tab - they are sampled
  and listed in Lifeline_Facilities.
- Background run: the classification and "Download data now" run in a QGIS
  task. The dialog's buttons are disabled meanwhile; the layers, map book,
  report and summary are added when it finishes.
- Downloads side by side: the services, the pages of a large ArcGIS layer
  (after a count query) and the NSI tiles are fetched at the same time (at
  most 6 requests in flight, 2 for Overpass).
- One-pass sampling: instead of reading a raster window and rasterising the
  area once per structure, a coarse wet mask finds the structures near the
  flood, all evaluation areas are rasterised together (areas whose rings
  overlap in separate passes, so each keeps its own cells) and velocity,
  arrival and D x V are read for every cell at once. The cells and values are
  the same as before.
- QA/Structure_Evaluation_Areas.gpkg: for every structure with a wet cell,
  the exact polygon its maximum depth and velocity were searched in - the
  outline grown by the 5-ft edge ring, or the disc around a point - with the
  reach in feet, the number of wet cells read, how they were read, the
  values found, the ACER zone and the ND outcome (shown as pink outlines).
  QA/Structure_Sampled_Cells.gpkg: the raster cells behind them (hidden
  by default; option).
- Re-use of the last evaluation ("Re-use the last run's downloads, inventory
  and raster sampling" on the Hazard Classification tab, on by default). A
  run compares the rasters (their contents), the chosen layers, the download
  choices and the settings that shape the sampling with the last run in this
  QGIS session; when unchanged, only the review decisions, the habitability
  rules, consequences, classification and outputs are redone. The summary and
  the run log say when an evaluation was re-used and from when.
- Sole access is now placed on the road network for every structure, so a
  structure marked habitable in a review has its answer without a full run.
- Removed: the D x V-bound and time-step ACER methods (Dmax / Vmax only since
  2.5.1), the time-step folder box, All_Consolidated and the EdgeBuf_* fields
  (see Structure_Evaluation_Areas), the POI workflow. The manual is
  README.txt in the plugin folder (also shown here).

NEW IN 2.5.1
- Fix: importing (DSS-WISE, HEC-RAS plan, any GDAL step) stopped with
  "Cannot call gdal.DontUseExceptions() at that point since the stack of
  error handlers is: ogr gdal" on GDAL 3.7+ - the GDAL / OGR exception
  settings are now restored in the reverse order.
- ACER zones come only from max depth with max velocity (Dmax / Vmax). The
  D x V bound and time-step methods (2.0.0) are no longer offered; a D x V
  raster is still used for flood severity in the loss-of-life estimate.
  Without a velocity raster the depth-only (1D) criterion is used (a
  DSS-WISE package without velocity is imported as 1D).
- Hazard Classification tab: the Hydraulics group is gone (ACER method,
  no-breach baseline and extra scenarios - the Processing algorithm still
  takes a baseline); the breach model (2D / 1D) is on the Setup & Run tab.
  Groups: 1 Criteria, 2 Report, 3 Consequences, 4 EAP outputs; Criteria,
  Consequences and EAP outputs are shown with "Show advanced options".
- Structures with an ND GIS Hub site address are habitable, ACER type
  "Habitable" (house-on-foundation curve, TM 11 Fig. 2; a manufactured-home
  source keeps the mobile-home curve). A Place_Type such as Parking or
  Vacant is kept in the notes and the structure is listed on the Review
  tab. Structures without an ND address follow the earlier rules.
- FEMA_Category (FEMA USA Structures OCC_CLS / PRIM_OCC, else the NSI
  Hazus occupancy) replaces Category in the result layers, report, EAP
  list and Review tab - for reference only. The Review tab has "Habitable
  (now)" and "Set habitable"; saved decisions still apply.
- Data & Output tab: output options, study area, structures,
  roads / rail / crossings, dams / lifelines / other layers and the EAP
  channel. One set of choices drives Main Consolidation, the
  classification and "Download data now" (outputs/Data/<Dam>_<data>.gpkg,
  Downloaded_Data.csv, "Downloaded data" layer group). Study area =
  depth raster extent + buffer; flooded area = wet depth cells + flood
  buffer (roads, rail and NHD flowlines keep the study area). The saved
  layers are never auto-detected as inputs. "Edit data sources..." points
  a download at a work-server service.
- Self-test: expects the 5 Processing algorithms (HEC-RAS plan added in
  2.5.0 made the check fail).

NEW IN 2.5.0
- Emergency Action Plan outputs (Hazard Classification tab, group 6): each
  one can be switched off, or all of them with "Make Emergency Action Plan
  outputs".
  * Inundation map book: EAP/EAP_Map_Book.pdf - an index page and one
    Letter sheet per part of the flooded area (1 in = 500 ft ... 1 mile,
    default 1,000 ft), numbered in the order the flood reaches them, with
    arrival-time bands, contours, closed roads and the notification items
    (numbers = Order of EAP_Notification_List) listed beside each map.
    The layout stays in the Layout Manager for editing.
  * Travel-time table: EAP/EAP_Travel_Time (GPKG / CSV / XLSX) - river
    miles from the dam, arrival, peak depth and time of peak at stations
    every 0.5 mi, towns (OpenStreetMap), bridges / crossings, closed roads
    and other dams at their river mile. Uses your centerline layer, else
    USGS NHD flowlines traced downstream from the dam's NID point.
  * Notification list: Priority column - "1" for schools, child care, care
    homes and hospitals, "2" for emergency response and utilities.
- Campgrounds and RV parks (OpenStreetMap) are overnight-recreation
  structures (5.2.1.1): the whole area is evaluated with the mobile-home
  curve; persons = sites x persons per dwelling (one party when unknown).
- Schools and child care (OpenStreetMap) are listed as vulnerable
  occupants (judgment item, EAP priority); they never change the class.
- Classification margin (5.3(f)): how much deeper the flood must be at the
  controlling structure for the dam to become HIGH, or how much shallower
  for the structures to stop making it HIGH.
- National Inventory of Dams: this dam's hazard potential is compared with
  the result (enter the NID ID on the Data & Output tab).
- Changes since the last run: QA/Changes_Since_Last_Run.csv lists
  structures newly flooded / no longer flooded, zone, outcome, depth and
  persons changes, roads, lifelines and changed settings / rasters.
- Future development (5.3(b)): give a parcel layer - vacant parcels up to
  5 ac (adjustable) where a new house would be in the danger zone are
  listed (Future_Development_Parcels, judgment item).
- HEC-RAS plan tab (and Processing "Import HEC-RAS plan results"): reads
  the 2D results of a HEC-RAS 6.x plan (.p##.hdf) - max depth (with the
  terrain raster: WSE minus terrain), velocity, arrival, water surface and
  time of peak - without the RAS Mapper export. Check the first results
  against RAS Mapper.
- A download that fails uses the latest earlier download of the same area
  (the date is in the summary and run log).

NEW IN 2.3.0
- Rasters found automatically in the dam folder ("Find rasters").
- "Check inputs" and "Run everything" buttons (one progress bar).
- Main Consolidation needs no project layers: NSI structures and ND GIS
  Hub address points are downloaded when none are loaded, and the FEMA USA
  Structures outlines are downloaded once and saved as
  <Dam>_FEMA_Structures.gpkg when that file is missing (both options on
  the Setup tab). The classification does the same.
- Roads and traffic: without a road layer, the ND GIS Hub NG9-1-1 road
  centerlines are downloaded with FHWA HPMS (newest year) and ND county-
  road AADT for the traffic counts.
- Bridges, culverts and crossings: National Bridge Inventory bridges and
  culverts (ADT, owner, year built, scour code) and OpenStreetMap
  culverts, fords / low-water crossings and railroad bridges in the flood
  are listed (Bridges_Culverts_Crossings, sorted by ADT), as a criteria row
  and a judgment item (confirm deck / road overtopping or washout), and in
  the EAP notification list. They do not change the class by themselves.
- Result panel instead of the pop-up; criteria and consequence settings
  are under "Show advanced options" (ND defaults preset).
- Tidier outputs: outputs/Classification (result layers), outputs/EAP,
  outputs/QA (Inventory_QA, sample points, model-extent check) and
  outputs/Consolidation (Main / POI Consolidation layers and hazard
  rasters). FEMA_Structures.gpkg, <Dam>_POI.gpkg, the review decisions,
  the summary, the PDF report and the run log stay in outputs. Copies left
  in outputs by earlier versions are removed. CSV / XLSX copies are
  optional; the pre-2.0 All_Consolidated join layers are off by default.
- Data-source addresses can be changed without a new version: Readme tab
  "Edit data sources..." opens ND_Dam_Hazard_Helper_data_sources.json in
  the QGIS profile folder (restart QGIS after a change).
- Processing toolbox: "Batch hazard classification (folder of dams)" runs
  every dam folder in a folder and writes Batch_Hazard_Classification.csv;
  the single-dam algorithm finds the rasters when Depth is left empty.

NEW IN 2.2.0
- Building outlines: the outline file is now FEMA_Structures.gpkg (or
  {Dam}_FEMA_Structures.gpkg) in the outputs folder - it was called
  OSM_Outlines.gpkg, but in ND it holds the FEMA USA Structures layer.
  The old name is still read (the log asks to rename it). Outputs:
  {Dam}_FEMA_Structures_Consolidated, {Dam}_FEMA_Structures_Sample_Points
  (Structure_ID = BUILD_ID), All_Consolidated columns FEMA_*. The "FEMA
  USA Structures" download only runs when that file is missing.
- FEMA point layers are no longer processed (Main Consolidation and
  Hazard Classification); a point layer named "FEMA ..." is ignored.
- Dams in series (ND 5.3(c)): National Inventory of Dams points (USACE
  service, Esri mirror as fallback) and / or your own dam layer. Dams with
  depth > 0 within 500 ft of their point are listed (Dams_In_Series layer,
  criteria row 5.3(c), judgment item with NID ID, hazard potential and
  depth). The dam being classified is left out (its NID ID, or its name).
- Lifeline facilities (ND 5.2.2) from data instead of name keywords: ND
  GIS Hub hospitals and long-term care, HIFLD fire / EMS / EOC / law
  enforcement / wastewater (v2.9: from your data folder - download them at
  https://www.hsdl.org/hifld; HIFLD Open closed in Aug 2025), EIA power plants, OpenStreetMap (substations, water plants
  and the rest) and / or your own layer. Duplicates within 300 ft are
  merged. Flooded lifelines (charged to the breach with a baseline) make
  the dam at least ND Medium unless switched off; care homes are a
  judgment item. Layer Lifeline_Facilities.
- Model extent check: flooded cells on the edge of the depth raster (or
  of the model boundary polygon you give) are reported per side with
  depth, arrival and - with a baseline - how much the breach adds there
  (Model_Boundary_Check layer, summary, judgment item).
- Emergency Action Plan outputs (Report options): EAP_Notification_List
  (GPKG / CSV / XLSX) - flooded or cut-off structures with address (ND
  GIS Hub NENA first, then FEMA PROP_ADDR), persons, depth, ACER zone,
  lifelines, other dams and road closures, sorted by arrival time with
  lat / lon; EAP_Arrival_Bands.tif (0-0.5, 0.5-1, 1-2, 2-4, 4-8, 8-24,
  > 24 h) and EAP_Arrival_Contours.gpkg, in an "Emergency Action Plan"
  layer group.

NEW IN 2.1.0
- Review tab: a structure with a saved decision is no longer listed after
  the next run ("Show structures already reviewed" brings it back, with
  its saved decision). "Set category" = habitability (dwelling, occupied,
  unverified, not habitable), "Set house / mobile home" = the ACER curve;
  "Automatic result is correct" records a check without changing
  anything. "List every flooded structure" lists all of them.
  Double-click a row to zoom. Unsaved edits survive ticking an option.
- PDF report rebuilt on every run: page 1 has the ND Dam Safety Standards
  REG_05.2026 Sec. 5.2 criteria table (each criterion, what was found,
  High / Medium / judgment) next to ACER TM 11 Table 1 (lives in
  jeopardy) and the scenarios; the tables list overtopped roads / rail
  and every flooded habitable structure with its ND outcome AND ACER
  zone. If the old PDF (or any output) is open in another program the
  new one gets a time-stamped name and the completion message says so;
  "Open PDF report" opens the one just written.
- Layer panel: every layer the plugin adds goes into
  "ND Dam Hazard - <dam folder>" with sub-groups Classification results,
  Sample locations, Points of interest, Consolidated structures, ACER
  hazard zones, Hydraulic rasters, DSS-WISE Lite. A re-run replaces the
  layers instead of adding them again; empty groups are removed.
- <dam>_POI.gpkg is loaded after Main Consolidation (Points of interest).
  Once it exists it is never overwritten, so your edits are kept; delete
  it to copy the source POI layer again.
- <dam>_OSM_Sample_Points.gpkg: where each OSM outline's Hmax / Vmax /
  Arrival were read (the outline's point, or the nearest valid cell within
  the sampling buffer - Offset_ft) and where the edge-ring maxima were.
  Structure_Sample_Points.gpkg: the cells behind each flooded structure's
  maximum depth, velocity, D x V, earliest arrival and controlling ACER
  zone. Both are added switched off (Sample locations group).
- Hazard_Classification_Run_Log.txt: settings, messages and result of
  every classification run (also a failed one), for the submittal file.
- Accuracy: an AADT must belong to the road (runs along it, same
  direction, route numbers / names do not clearly differ) - no longer
  taken from a crossing street or a parallel highway; a US / State
  highway without AADT is a judgment item; abandoned / removed rail and rail trails (NARN NET
  A, R, T) are not railroads; trains/day only from crossings on the same
  line or subdivision; a roof outline joins the building it overlaps
  most; the 1D 2-ft rule has its own borderline check.

NEW IN 2.0.0
- ACER zones with depth and velocity from the same moment. Max depth and
  max velocity usually occur at different times; pairing them (the only
  method before 2.0) overstates the hazard. Setup tab, optional:
    * Depth x Velocity (max) raster (RAS Mapper "Depth x Velocity",
      DSS-WISE DV): the worst zone over every depth / velocity pair the
      three maxima allow - never worse than max/max, usually much better;
      velocity may then be left out (conservative: up to each figure's
      velocity range)
    * folder of time-step rasters "Depth (<time>).tif" / "Velocity
      (<time>).tif" or "Depth_0100.tif" / "Velocity_0100.tif" (RAS Mapper
      export): worst zone over the output times. Where the exported times
      miss a cell's maximum depth, the Dmax-based method is applied there
      as well (logged; without a max velocity raster the velocity there is
      taken up to each figure's range). HEC-RAS results are used through
      these RAS Mapper exports; the plan .hdf file is not read directly.
  Used for the hazard GeoTIFFs (now also Hazard_Vehicles.tif), structures
  and road vehicle zones. The arrival raster is optional; the velocity
  raster may be left out with a D x V raster, time steps or the 1D
  (depth-only) criterion.
- Scenarios (Hazard Classification tab): a no-breach baseline makes the
  classification incremental (FEMA P-333): consequences that occur without
  the failure are not charged to the dam. Charged: the breach floods a dry
  place, makes the outcome worse (e.g. economic loss -> probable loss of
  life), worsens the ACER zone or adds >= 1 ft; roads also when the
  vehicle zone gets worse; loss of access only when the breach causes it.
  Outcomes and zones are compared on the same basis on both sides: as
  found when both scenarios used the same ACER method and both (or
  neither) have a velocity raster, otherwise at max depth with max
  velocity; a side without velocity is then judged by depth only.
  More breach scenarios (sunny day, PMF, DSS-WISE ...) are classified in
  one run - each one's roads and rail are searched in its own flooded
  area - and the most severe governs. Scenario rasters in another CRS are copied into the
  first breach scenario's CRS (outputs/_reprojected).
- Consequences (screening): value of flooded structures and contents (NSI)
  and residential damage (USACE EGM 01-03 depth-damage, depth above the
  first floor); loss-of-life estimate with Graham (1999) DSO-99-06 fatality
  rates from flood severity (DV) and warning time (arrival time minus the
  warning time you enter).
- Borderline structures: depth margin to the next ACER boundary; within
  0.5 ft (adjustable) they are flagged (ND 5.3(f)) or, optionally, counted
  in the higher zone.
- Sole-access roads: the road network is cut where the flood starts and
  ends along each road; line ends that stop on another road without a
  shared vertex (T-junctions) are joined. Habitable structures whose own
  road is overtopped, or whose every mapped route out of the model area is
  flooded, are listed (Access_Cut_Off) and make the dam at least ND Medium
  (5.2.1.2) unless switched off; a structure whose mapped roads never leave
  the model area, even dry, is reported as unknown.
- Building outlines: FEMA USA Structures download and any extra outline
  layer; outlines overlapping an existing building are merged into it.
  Inventory_QA.csv lists outlines without any point (possible unmapped
  dwelling) and points without an outline.
- Review tab: step through unverified / judgment / borderline / moved /
  lifeline / cut-off structures, zoom to them, record decisions (category,
  house or mobile home, persons, note). Saved in
  outputs/Structure_Review_Decisions.csv (with reviewer and date) and
  applied on every later run; a saved decision can be removed. A copy
  saved by Excel (ANSI) is read; a file that cannot be read is never
  overwritten; decisions that no longer match a structure are reported.
- PDF report: Hazard_Classification_Report.pdf from a print layout kept in
  the project's Layout Manager (overview map, zoom maps of the high /
  judgment areas, legend, scale bar in feet / miles, summary, structure
  table). The depth raster shown is added to the project so the saved
  layout keeps it.
- DSS-WISE Lite tab: import a Simulation Results Package (zip or folder);
  depth, velocity, D x V, arrival and DEM rasters are recognised from the
  file names (units too: _in_Ft, _in_M, _in_M_per_Sec, _in_Hrs ...), shown
  for confirmation, converted to ft / ft/s / ft2/s / hours and used as the
  Setup rasters or as an extra scenario; HCOM population table and
  discharge hydrographs are summarised; hazard-zone polygons are loaded.
  Units a file name does not state are marked "assumed" and must be
  confirmed (Processing: unit parameters or "accept US units"); when two
  files have the same role, the one stating its units / the UTM copy is
  used and the choice is reported. The DSS-WISE package layout is not
  formally published - check the table against the DSS-WISE report.
- Processing toolbox: "ND Dam Hazard" provider with ACER zone rasters
  (runs in the background), hazard classification and DSS-WISE import -
  usable in models, batch mode and qgis_process.
- All settings (rasters, options, scenarios) are saved in the QGIS project
  and restored when the plugin is opened again; opening another project
  shows that project's settings.

NEW IN 1.4.0 - STUDY AREA, ND GIS HUB ADDRESS POINTS, QGIS 3.28 - 4.x
- Study area = the Depth raster's extent + 25 ft (Setup tab, adjustable).
  OSM outlines, NSI, ND GIS Hub, FEMA and POI features and all downloads
  are limited to it: an outline is kept if any part of it is inside, a
  point if it is inside. Everything inside is kept - also where the
  rasters have no value (Hmax/Vmax/Arrival left empty) - unless "Keep
  features ... that have no flood values" is switched off. The test is
  done in the raster's CRS, so it is exact for layers in any CRS.
- ND GIS Hub: the NDGISHUB group is the ND GIS Hub "NDGISHUB SiteStructure
  Address Points" layer (NENA NG9-1-1 site / structure address points).
  It is recognised under the Hub name, the service layer name
  "DBO_SiteStructureAddressPoints", or any "Address Points" name.
  Place_Type is read (residence / apartment / MOBILE HOME / business /
  church / PARKING / Vacant ...), street-name fields are ignored, and
  unit addresses (Unit = 1, 2, ...) count as separate dwellings.
  Address points placed at the driveway or parcel (Placement other than
  "Structure") are moved onto the nearest building outline within 100 ft
  that has no address yet. Hazard Classification downloads the points for
  the study area when no ND GIS Hub layer is loaded.
- The 5-ft edge-buffer ring is built in the raster's CRS: correct width
  also for OSM outlines in EPSG:4326. Web Mercator / geographic rasters get
  latitude-corrected distances (a warning recommends a projected CRS).
- Runs on QGIS 3.28 LTR through QGIS 4.x (Qt6) on Windows, macOS and
  Linux: version-independent enum / field-type names, QAction from the
  right module, Python 3.9 compatible, numpy 1.x and 2.x.
- Faster on big study areas: dry outlines and structures are recognised
  from one small raster read before any geometry work.

NEW IN 1.3.1 - ACER CURVES
- Passenger-vehicle curves (TM 11 Figure 4) added: overtopped paved roads
  get a vehicle danger zone and TM 11 counts motorists.
- CORRECTION: the mobile-home High boundary had a typo in its x^4 term
  (9.65e-05 instead of 9.65e-06). The old curve rose to 3.9 ft at 12 ft/s
  and 6.3 ft at 15 ft/s (Figure 3: ~2.1 and ~1.9 ft), rating mobile homes
  in fast water as Judgment instead of High above about 6 ft/s.
  Regenerate Hazard_MobileHomes.tif for affected studies.
- Velocities beyond a figure's range (houses 25 ft/s, mobile homes and
  vehicles 16 ft/s) are evaluated at the range limit instead of
  extrapolating the quartics.

NEW IN 1.3.0 - HAZARD CLASSIFICATION TAB
- Finds habitable structures in the flooded area: merges OSM building
  outlines with NSI, FEMA and ND GIS Hub points, and (optionally) downloads
  the USACE National Structure Inventory for the flooded extent.
- Habitability follows ND REG_05.2026 5.2.1.1 (dwellings, trailers and
  mobile homes, buildings regularly occupied, overnight recreation).
  Sheds, garages, barns, bins etc. are excluded; untagged outlines smaller
  than the minimum area are treated as outbuildings; anything uncertain is
  listed as "Unverified" for review.
- Structure type: mobile home if NSI RES2 / building type H, OSM
  static_caravan, or "manufactured / mobile / trailer" in the attributes;
  otherwise house on foundation. Each structure is classified with ITS
  curve, cell by cell over the outline + 5-ft buffer (depth and velocity
  from the same cell), keeping the worst zone.
- Roads: every road line is sampled every 10 ft; overtopped stretches get
  road class (Interstate / US / State / local / private), paved status,
  bridge flag and the MOST RECENT AADT from (a) the road layer itself,
  including AADT_2023-style columns, (b) an optional AADT layer (NDDOT
  counts, HPMS) within 300 ft, (c) FRA crossing AADT within 100 ft.
- Rail: the national rail network (FRA/BTS) and FRA grade crossings are
  downloaded for the area (or use your own layers); trains/day come from
  crossings of the same railroad within 3 miles; passenger lines flagged.
- ND classification per REG_05.2026 Section 5.2 and ACER TM 11 Table 1
  classification by lives in jeopardy, with the reasons and a list of
  items that need engineering judgment.
- Outputs: Habitable_Structures, Roads_Inundated, Rail_Inundated
  (GPKG/CSV/XLSX, styled in the project) and
  Hazard_Classification_Summary (TXT/XLSX/CSV). Downloads are cached in
  outputs/_downloads with the date.

ND CRITERIA APPLIED (REG_05.2026, effective 04/01/2026, Section 5.2)
- HIGH: any habitable structure in the high danger zone of Figs 5.1/5.2
  (houses / mobile homes, Reclamation 1988), or deeper than 2 ft for a 1D
  model; judgment-zone structures when the conservative option is on
  (5.3(f)); Interstates or roads with AADT > 2,000 overtopped at any depth;
  passenger rail or freight lines with >= 10 trains/day.
- MEDIUM: habitable structures inundated to any degree; US/State highways
  or roads with AADT 500-2,000; any railroad; lifeline facilities.
- LOW: otherwise (private roads, AADT < 500, uninhabited buildings).
- Always reviewed by the engineer: judgment-zone structures, sole-access
  roads, lifeline facilities, bridges, future development, dams in series.

OFFLINE DATA PACK (2.6.1) - running without internet access
- The pack holds every data set the plugin downloads, for all of North
  Dakota (its bounding box plus 0.1 degree, about 5 miles, so the 3-mile
  grade-crossing and 2-mile town searches still work at the state line):
  NSI, ND GIS Hub address points and roads, FEMA USA Structures, NID,
  rail and grade crossings, HPMS / NDDOT / county-road traffic, NBI,
  lifeline facilities (ND GIS Hub, EIA; v2.9: no HIFLD), USGS flowlines
  (2.29.0: 3DHP; before: NHD; name and type fields) and the OpenStreetMap lifelines, crossings,
  campgrounds and towns. Data sets from services that only cover North
  Dakota have nothing beyond it, online or in the pack.
- Build it on a computer that is online, either
    Data & Output tab > 7. Offline data pack > Build offline pack...
  or, without QGIS, with any Python 3.7+ (QGIS's own Python too):
    python <plugin folder>/build_nd_pack.py --out nd_offline_pack.zip
  (--list shows the data sets; --only / --skip pick some, e.g. --skip nhd;
  --refresh fetches finished ones again; --sources <file> uses your
  data-source file's addresses). Expect an hour or more and a large
  file (hundreds of MB or more); FEMA USA Structures and NHD are the
  largest - --skip hydro leaves the 3DHP streams out if you always give a centerline. A stopped build goes on
  where it stopped when run again with the same --out (the finished data
  sets are kept in <name>_work/ next to the zip). Data sets that failed
  are listed at the end and in the zip's README.txt.
- On the offline computer: Data & Output tab > 7. Offline data pack >
  Browse..., pick the zip (unpacked once, next to the zip - or in
  ~/.nd_dam_hazard_helper/offline_packs when that folder is read-only),
  and tick "Offline only". The choice belongs to the computer, not the
  project (QGIS user settings; environment variables
  ND_DAM_HAZARD_OFFLINE_PACK / ND_DAM_HAZARD_OFFLINE_ONLY override it for
  scripts). Processing algorithms use it too.
- A run then reads each data set from the pack for the study area (same
  features as the service's area query; written to
  outputs/_downloads/_pack), and the run log, the data-source list and
  Check inputs say which data sets came from the pack and when they were
  fetched. Not "Offline only": data sets missing from the pack are
  downloaded, and a study area reaching beyond the pack's area uses the
  service (the pack if the service fails). "Offline only": nothing is
  requested from the internet; a data set missing from the pack falls
  back to an earlier download of the same area, if there is one.
- The pack is a snapshot: rebuild it (or --refresh a data set) to pick
  up newer data.

CONSEQUENCE SCREENING SOURCES
- Graham, W.J. (1999) USBR DSO-99-06, Table 7 fatality rates; severity:
  DV < 50 ft2/s low, else medium (10 ft depth rule without velocity);
  optional RCEM 2015 bands (DV 50 / 160 ft2/s).
- USACE EGM 01-03 generic residential depth-damage (no basement), applied
  to NSI RES1 structures; other occupancies: value exposed only.
- These are screening numbers, not a risk analysis.

ACER TM 11 (1988)
- Lives in jeopardy = 3 persons per dwelling (or NSI population for
  multi-family / occupied buildings) in the high (and optionally judgment)
  zone, plus motorists on paved roads. Table 1: Low 0, Significant 1-6,
  High > 6.
- Motorists: vehicles on the flooded stretch = AADT / 24 x length /
  speed, x persons per vehicle, counted where the Figure 4 passenger-
  vehicle zone is high (or judgment, per the conservative option) on paved
  roads.

NEW IN 1.2.0 (performance and stability)
- Hazard rasters are processed in strips: memory use stays flat on very
  large depth grids, and the output GeoTIFFs are tiled with overviews.
- Raster sampling reads each raster once through GDAL and finds the
  nearest valid cell with vectorised math (much faster outside the
  flooded area).
- Edge-buffer zones are fixed (they previously failed and were skipped)
  and are computed by rasterising each ring instead of cell-by-cell tests.
- The All_Consolidated join uses the spatial index only (no brute-force
  scan) and is computed once for both All_Consolidated outputs.
- The sampling buffer is converted from feet to each raster's units.
- Layers in different CRSs are reprojected correctly.
- Re-running no longer duplicates layers or re-reads the plugin's own
  *_Consolidated outputs; locked output files get a time-stamped name.
- Cancel works in every stage; the progress bar never moves backwards.
- Readme tab: "Run Self-Test" checks the install on synthetic data.

BUTTON STATES
- "Run Main Consolidation" turns green once the dam folder AND all three
  rasters are selected. It can only be clicked while green.
- "Run POI Consolidation" turns green once the *_POI.gpkg file exists
  in the output folder (created by Main Consolidation).

INSTALL
1. QGIS > Plugins > Manage and Install Plugins > Install from ZIP
   - or -
   Copy the unzipped ND_Dam_Hazard_Helper folder to the profile's
   python/plugins folder:
   Windows: %APPDATA%\\QGIS\\QGIS3\\profiles\\default\\python\\plugins\\
   macOS:   ~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/
   Linux:   ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/
   ("Install from ZIP" always picks the right folder for your QGIS version.)
2. Restart QGIS
3. Enable the plugin in Plugins > Manage and Install Plugins
4. Readme tab > "Run Self-Test" confirms the install.

SUITE DATA AND SERVERS / GET DATA FOR THIS DAM (2.17.0)
- Plugins > ND Dam Suite > Suite data and servers... (the same window in the three
  dam plugins; also in this plugin's menu and "Data and servers..." in group 8 of the
  Hazard Classification tab, which opens it on the current dam folder).
- Set the suite data folder once per computer: it holds the statewide offline pack
  (nd_offline_pack.zip - this plugin reads it when no pack is chosen on the Data &
  Output tab), the shared terrain tile cache and drop-in folders (dem/, nlcd/, dwr/,
  hifld/). Set the Dams folder (one folder per dam) once for the three plugins.
- Servers: for every kind of data, the WMS / WMTS / WCS or ArcGIS REST service it comes
  from. "Add server..." takes any WMS or ArcGIS REST address and lists its layers.
- "Get data for this dam" (or "... for all dams"; Processing: ndhazard:getdamdata)
  clips DEM, land cover, structures, address points, roads, rail, crossings, bridges,
  lifelines, NID / DWR dams, DWR dam lines, NHD flowlines and waterbodies, flood zones
  and counties into <dam folder>/data/ and records them in dam.json's data section.
- Then, in this plugin, every data set of the Data & Output tab you left empty is taken
  from there, marked "(from dam folder)" - only when the dam's data hold the whole study
  area (else it is read or downloaded as before, and the log says why). A layer you
  chose and the data-folder search's picks always win; the run log lists what was used.
  The NID ID box is filled from dam.json when you choose the dam folder.

USAGE
1. Browse (or type) the Dam Project Folder; an "outputs" subfolder is
   created there.
2. Select Depth, Velocity, and Arrival rasters.
3. Click "Run Main Consolidation" (button turns green when ready).
4. Place FEMA_Structures.gpkg (or {Dam}_FEMA_Structures.gpkg) in the
   outputs folder BEFORE running Main Consolidation to enable the
   building-outline stages (OSM_Outlines.gpkg is still read).
5. Optional: Edit the *_POI.gpkg, save, click "Run POI Consolidation".
6. Hazard Classification tab: load a road layer (NDDOT roads, HPMS, OSM)
   and optionally an AADT layer, choose the model type, then click
   "Run Hazard Classification". Internet is used for NSI, ND GIS Hub
   address points, FEMA USA Structures (optional, only without
   FEMA_Structures.gpkg), rail lines and crossings, the National
   Inventory of Dams and lifeline facilities unless those options are
   switched off. Enter the dam's NID ID so it is not listed as a dam in
   series.
7. Review tab: decide the listed structures, "Save and re-run".
8. DSS-WISE Lite tab: import a DSS-WISE package instead of (or besides)
   HEC-RAS rasters.
   If the POI layer still has unsaved edits in QGIS you will be asked
   whether to save them first.

REQUIREMENTS
- QGIS 3.28 or later (including QGIS 4 / Qt6) on Windows, macOS or Linux.
  numpy and GDAL are bundled with QGIS.
- Point layers whose names contain "NSI", "NDGISHUB"/"GISHUB" or
  "SiteStructure" / "Address Points", and a layer whose name contains the
  word "POI", must be loaded in the project for consolidation. Point
  layers named "FEMA ..." are ignored (v2.2). "NSI" must start a word (so names
  such as "Density" or "Transit" are ignored) and "POI" must be a whole
  word (so "Points" is ignored). Layers with "Consolidated" in the name
  are treated as outputs and ignored.
- The sampling buffer (ft), the study-area margin and the 5-ft edge
  buffer are converted to the raster CRS units (feet, metres, or degrees;
  Web Mercator corrected for latitude). A projected CRS (UTM or ND State
  Plane) is recommended for the rasters.

VERIFIED CLASSIFICATION REFERENCE
- House on foundation (Fig 2): H=3 ft, V=6 fps -> Class 2 (Judgment);
  H=4 ft, V=4 fps -> Class 2 (Judgment). At V=6: low ~2.26 ft, high ~5.21 ft.
- Mobile home (Fig 3): H=3 ft, V=12 fps -> Class 1 (High).
  At V=12: low ~1.45 ft, high ~2.06 ft.
- Passenger vehicle (Fig 4): H=2.5 ft, V=2 fps -> Class 2 (Judgment).
  At V=6: low ~1.48 ft, high ~2.34 ft.
- The Figure 4 curves were traced from the TM 11 scan by image analysis;
  the same procedure reproduces the verified house curves within 0.05 ft.

RUNTIME VERIFICATION
Each run prints a threshold table and known-answer tests to the QGIS Log
Messages panel. Expected known tests: found 2, found 2, mobile 1, vehicle 2.
The "Run Self-Test" button below runs the whole pipeline on a small
synthetic data set in a temporary folder (your project is not touched).

License: GNU GPL v3
