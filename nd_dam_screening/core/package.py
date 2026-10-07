"""Reading a data package GeoPackage without QGIS (sqlite3): its layer list and, for a package
exported by ND Dam Hazard (v2.10+, 'Export data package for ND Dam Breach Screening'), the
table suite_export_info (format, source, area, counts, what is missing). Used by tool 0."""
import json
import os
import sqlite3

INFO_TABLE = "suite_export_info"
ND_FORMAT = "nd_dam_suite/screening_package"


class PackageError(ValueError):
    pass


def _connect(path):
    if not path or not os.path.isfile(path):
        raise PackageError("Data package not found: {}".format(path))
    uri = "file:{}?mode=ro".format(os.path.abspath(path).replace("?", "%3f").replace("#", "%23"))
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        raise PackageError("{} could not be opened: {}".format(path, e))


def gpkg_layers(path):
    """{table name: {"data_type", "srs_id", "geometry_type"}} of a GeoPackage."""
    con = _connect(path)
    try:
        try:
            gcols = {r[0]: r[1] for r in con.execute(
                "SELECT table_name, geometry_type_name FROM gpkg_geometry_columns")}
            rows = con.execute("SELECT table_name, data_type, srs_id FROM gpkg_contents").fetchall()
        except sqlite3.Error as e:
            raise PackageError("{} is not a GeoPackage: {}".format(path, e))
        return {name: {"data_type": dt, "srs_id": srs, "geometry_type": gcols.get(name)}
                for name, dt, srs in rows}
    finally:
        con.close()


def export_info(path):
    """{key: value} of the package's suite_export_info table ({} when it has none)."""
    con = _connect(path)
    try:
        try:
            rows = con.execute('SELECT "key", "value" FROM "{}"'.format(INFO_TABLE)).fetchall()
        except sqlite3.Error:
            return {}
        return {str(k): v for k, v in rows if k is not None}
    finally:
        con.close()


def _json(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def info_lines(info):
    """Log lines describing where an ND Dam Hazard package came from."""
    if not info:
        return ["(no suite_export_info table: not an ND Dam Hazard export - used by layer names)"]
    out = []
    if info.get("format") and info.get("format") != ND_FORMAT:
        out.append("format {} (expected {}) - used by layer names".format(info.get("format"), ND_FORMAT))
    for k in ("created_by", "created", "source", "area_lonlat", "crs"):
        if info.get(k):
            out.append("{}: {}".format(k, info[k]))
    counts = _json(info.get("counts"), {})
    if counts:
        out.append("counts: " + ", ".join("{} {}".format(k, v) for k, v in sorted(counts.items())))
    for n in _json(info.get("notes"), []):
        out.append("note: {}".format(n))
    return out


def missing_lines(info):
    return [str(m) for m in _json((info or {}).get("missing"), [])]
