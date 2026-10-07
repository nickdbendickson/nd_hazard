"""Download a DEM for one dam, one North Dakota county, or the state.

Source is USGS 3DEP, or ND DWR QL2 LiDAR from https://lidar.dwr.nd.gov: tiles for a dam,
county or the state over HTTPS (0.18.1), or a package staged on the map site (public lidar FTP).
"""
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterEnum, QgsProcessingParameterFile,
                       QgsProcessingParameterString, QgsProcessingParameterNumber,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterBoolean,
                       QgsProcessingException, QgsProcessingOutputFile)

import os

from ..compat import FILE_FOLDER, NUM_DOUBLE
from ..core import dem_download as dd


class DownloadDem(QgsProcessingAlgorithm):
    def name(self):
        return "downloaddem"

    def displayName(self):
        return "20 - Download a DEM (dam, county, or state)"

    def group(self):
        return "Dam screening"

    def groupId(self):
        return "dams"

    def createInstance(self):
        return DownloadDem()

    def shortHelpString(self):
        return ("USGS 3DEP: metres in EPSG:5070 for a dam buffer, one North Dakota county, or "
                "the state. ND DWR LiDAR: LiDAR_NorthDakota_QL2 tiles from https://lidar.dwr.nd.gov. "
                "With the FTP link empty the tool finds every tile that touches the dam buffer, "
                "the county or the state in the statewide tile index and downloads them over "
                "HTTPS (0.18.1; works where FTP is blocked). With an FTP link (tiles chosen on the "
                "map site, Copy ftp link) it pulls that package over FTP instead. It pulls "
                "ASCII_Grids_ft (feet, converted to metres in a separate VRT) and DEM_Imagine (1 m, metres). The two are never mosaicked together; DEM_Imagine is used when both are fetched. A tile that fails to download stops the tool (failed_tiles.txt) instead of leaving a hole; run it again to resume. Guest login is swc / water.\n"
                "0.16.0: the VRT is named after the dam ID or county (<dam ID>_dem_10m.vrt, "
                "Cass_dem_10m.vrt), so a second dam in the same folder no longer overwrites the "
                "first. With the Dams folder and a Dam ID (a dam folder that exists) the DEM is "
                "written into <dam folder>/data/ and recorded in dam.json's data section, where "
                "every tool of the three ND dam plugins finds it; an empty dam point is taken from "
                "dam.json. With a suite data folder (Plugins > ND Dam Suite > Suite data and "
                "servers) 3DEP tiles come from the shared terrain cache: a tile is downloaded once "
                "for every dam, tool and work folder.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterEnum(
            "SOURCE", "Source", ["USGS 3DEP", "ND DWR LiDAR (lidar.dwr.nd.gov)"], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            "SCOPE", "Area", ["Dam", "County", "State"], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            "DAM_POINT", "Dam point, longitude,latitude (area Dam)", optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            "BUFFER_MI", "Buffer around the dam (miles)", NUM_DOUBLE, 10.0, minValue=1.0))
        self.addParameter(QgsProcessingParameterString(
            "COUNTY", "County (area County)", optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            "CELL", "3DEP cell size (m)", NUM_DOUBLE, 10.0, minValue=1.0))
        self.addParameter(QgsProcessingParameterString(
            "DWR_PATH", "ND LiDAR FTP link or /temp/<id> path (empty = tiles for the area over HTTPS)", optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            "ASCII_FT", "ND LiDAR: ASCII_Grids_ft (feet; converted to metres)", False))
        self.addParameter(QgsProcessingParameterBoolean(
            "IMAGINE", "ND LiDAR: DEM_Imagine", True))
        self.addParameter(QgsProcessingParameterString(
            "WORK_CRS", "Work CRS (empty = keep the source CRS)", optional=True))
        # 0.16.0: the dam the DEM is for (its dam folder gets the DEM; its point fills DAM_POINT)
        self.addParameter(QgsProcessingParameterFile(
            "DAMS_FOLDER", "Dams folder (one folder per dam; optional)", behavior=FILE_FOLDER,
            optional=True))
        self.addParameter(QgsProcessingParameterString(
            "DAM_ID", "Dam ID (names the file; with the Dams folder the DEM goes into "
            "<dam folder>/data/ and dam.json)", optional=True))
        self.addParameter(QgsProcessingParameterFolderDestination("OUT", "Output folder"))
        self.addOutput(QgsProcessingOutputFile("VRT", "DEM VRT"))

    def processAlgorithm(self, parameters, context, feedback):
        out = self.parameterAsString(parameters, "OUT", context)
        # 0.16.0: one dam folder -> the DEM goes into <dam folder>/data/ and dam.json
        dam_id = (self.parameterAsString(parameters, "DAM_ID", context) or "").strip()
        rec, dam_folder = _dam_record(self, parameters, context, feedback)
        if rec is not None:
            from ..damsuite import damdata as DD
            out = os.path.join(dam_folder, DD.DATA_DIR)
            dam_id = dam_id or str(rec.get("dam_id") or os.path.basename(dam_folder))
            feedback.pushInfo("dam folder {}: the DEM is written to {}".format(dam_folder, out))
        if self.parameterAsEnum(parameters, "SOURCE", context) == 1:
            products = []
            if self.parameterAsBool(parameters, "ASCII_FT", context):
                products.append("ASCII_Grids_ft")
            if self.parameterAsBool(parameters, "IMAGINE", context):
                products.append("DEM_Imagine")
            package = (self.parameterAsString(parameters, "DWR_PATH", context) or "").strip()
            try:
                if package:
                    vrt = dd.fetch_dwr(package, out, products, feedback)
                    n = None
                else:
                    scope = self.parameterAsEnum(parameters, "SCOPE", context)
                    box = None
                    if scope == 0:                       # 0.18.1: a dam point and buffer
                        lon, lat = _dam_point(self, parameters, context, feedback, rec)
                        box = dd.point_box_2266(
                            lon, lat, self.parameterAsDouble(parameters, "BUFFER_MI", context))
                    vrt, n = dd.fetch_dwr_area(
                        scope, self.parameterAsString(parameters, "COUNTY", context),
                        out, products, feedback, box_2266=box)
            except QgsProcessingException:
                raise
            except Exception as e:
                raise QgsProcessingException(str(e))
            vrt = _warp(vrt, out, parameters, context, feedback)
            feedback.pushInfo("ND LiDAR: {}{}".format(vrt, "" if n is None else " ({} tiles)".format(n)))
            _record(rec, vrt, "ND DWR LiDAR (lidar.dwr.nd.gov) by tool 20", feedback)
            return {"VRT": vrt, "OUT": out}
        scope = self.parameterAsEnum(parameters, "SCOPE", context)
        cell = self.parameterAsDouble(parameters, "CELL", context)
        if scope == 0:
            lon, lat = _dam_point(self, parameters, context, feedback, rec)
            extent = dd.point_box(lon, lat, self.parameterAsDouble(parameters, "BUFFER_MI", context))
            stem = dd.output_stem(0, cell, dam_id, point=(lon, lat))
        elif scope == 1:
            name = (self.parameterAsString(parameters, "COUNTY", context) or "").strip()
            box = dd.COUNTIES.get(name)
            if box is None:
                raise QgsProcessingException("Unknown county {!r}. One of: {}".format(
                    name, ", ".join(dd.county_names())))
            extent = dd.to_5070(*box)
            stem = dd.output_stem(1, cell, county=name)
        else:
            extent = dd.to_5070(*dd.STATE)
            stem = dd.output_stem(2, cell)
        try:
            vrt = dd.download(extent, cell, out, stem, feedback)
        except QgsProcessingException:
            raise
        except Exception as e:                    # a message, not a traceback (0.18.0)
            raise QgsProcessingException(str(e))
        vrt = _warp(vrt, out, parameters, context, feedback)
        feedback.pushInfo("DEM: {}".format(vrt))
        _record(rec, vrt, "USGS 3DEP {} m by tool 20".format(dd.cell_tag(cell).replace("p", ".")),
                feedback)
        return {"VRT": vrt, "OUT": out}


def _dam_point(alg, parameters, context, feedback, rec):
    """(lon, lat) from DAM_POINT, else from dam.json (0.18.1: shared by both sources)."""
    raw = (alg.parameterAsString(parameters, "DAM_POINT", context) or "").strip()
    if not raw and rec is not None and isinstance(rec.get("lon"), (int, float)) and \
            isinstance(rec.get("lat"), (int, float)):
        raw = "{},{}".format(rec.get("lon"), rec.get("lat"))
        feedback.pushInfo("dam point from dam.json: {}".format(raw))
    try:
        lon, lat = [float(x) for x in raw.replace(" ", "").split(",")[:2]]
    except ValueError:
        raise QgsProcessingException("Dam point must be longitude,latitude")
    return lon, lat


def _dam_record(alg, parameters, context, feedback):
    """(DamRecord, dam folder) when DAMS_FOLDER + DAM_ID name a dam folder that exists, else
    (None, ""). A dam.json that cannot be read is reported and left alone."""
    try:
        from .. import suite_data_tools as SDT
        from ..damsuite import dam_record as DR
        folder = SDT.dam_folder_of(alg, parameters, context)
        if not folder:
            return None, ""
        return DR.load(folder, create=True), folder
    except ValueError as e:
        feedback.pushInfo("dam.json not used: {}".format(e))
    except Exception as e:                                       # noqa - never stop the download
        feedback.pushInfo("dam folder not used: {}".format(e))
    return None, ""


def _record(rec, path, source, feedback):
    """0.16.0: the DEM as the dam's "dem" in dam.json's data section."""
    if rec is None:
        return
    try:
        from ..damsuite import damdata as DD
        DD.set_entry(rec, "dem", path, source)
        rec.save()
        feedback.pushInfo("dam.json: DEM recorded ({})".format(path))
    except Exception as e:                                       # noqa - the DEM is written anyway
        feedback.reportError("dam.json: the DEM was not recorded: {}".format(e))


def _warp(vrt, out, parameters, context, feedback):
    import os
    crs = ""
    try:
        crs = (parameters.get("WORK_CRS") or "")
        if not isinstance(crs, str):
            crs = ""
    except Exception:
        crs = ""
    crs = str(crs).strip()
    if not crs:
        return vrt
    epsg = crs.split(":")[-1]
    dest = os.path.join(out, "dem_{}.tif".format(epsg))
    if feedback is not None:
        feedback.pushInfo("warping to {}".format(crs))
    dd.warp_to(vrt, dest, epsg)
    return dest
