"""A minimal GeoPackage writer for the tests (sqlite3; GeoPackage binary header + ISO WKB, EPSG:5070):
write(path, {layer: (geometry_type, [(field, sql type)], [(geometry, {field: value})])}).
Geometries: ("Point", (x, y)), ("LineString", [(x, y), ...]), ("MultiPolygon", [[[ring]]])."""
import os
import sqlite3
import struct

CODES = {"Point": 1, "LineString": 2, "Polygon": 3, "MultiPoint": 4, "MultiLineString": 5, "MultiPolygon": 6}


def _wkb(kind, c):
    out = [struct.pack("<BI", 1, CODES[kind])]

    def seq(pts):
        out.append(struct.pack("<I", len(pts)))
        for p in pts:
            out.append(struct.pack("<dd", *p))
    if kind == "Point":
        out.append(struct.pack("<dd", *c))
    elif kind == "LineString":
        seq(c)
    elif kind == "Polygon":
        out.append(struct.pack("<I", len(c)))
        for r in c:
            seq(r)
    else:
        sub = kind[5:]
        out.append(struct.pack("<I", len(c)))
        for part in c:
            out.append(_wkb(sub, part))
    return b"".join(out)


def blob(geom, srs=5070):
    if geom is None:
        return None
    return struct.pack("<2sBBi", b"GP", 0, 1, srs) + _wkb(*geom)


def write(path, layers, info=None):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute("PRAGMA application_id = 1196444487")
    con.executescript("""
CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT, srs_id INTEGER PRIMARY KEY, organization TEXT,
  organization_coordsys_id INTEGER, definition TEXT, description TEXT);
CREATE TABLE gpkg_contents (table_name TEXT PRIMARY KEY, data_type TEXT, identifier TEXT, description TEXT,
  last_change TEXT, min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER);
CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, geometry_type_name TEXT,
  srs_id INTEGER, z TINYINT, m TINYINT);""")
    con.execute("INSERT INTO gpkg_spatial_ref_sys VALUES ('NAD83 / Conus Albers', 5070, 'EPSG', 5070, 'x', '')")
    for name, (gtype, fields, rows) in layers.items():
        cols = ", ".join('"{}" {}'.format(f, t) for f, t in fields)
        con.execute('CREATE TABLE "{}" (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom {}{})'.format(
            name, gtype, ", " + cols if cols else ""))
        con.execute("INSERT INTO gpkg_contents (table_name, data_type, srs_id) VALUES (?, 'features', 5070)",
                    (name,))
        con.execute("INSERT INTO gpkg_geometry_columns VALUES (?, 'geom', ?, 5070, 0, 0)", (name, gtype))
        for g, a in rows:
            con.execute('INSERT INTO "{}" (geom{}) VALUES (?{})'.format(
                name, "".join(', "{}"'.format(f) for f, _t in fields), ", ?" * len(fields)),
                [blob(g)] + [a.get(f) for f, _t in fields])
    if info is not None:
        con.execute('CREATE TABLE suite_export_info (fid INTEGER PRIMARY KEY, "key" TEXT, "value" TEXT)')
        con.execute("INSERT INTO gpkg_contents (table_name, data_type) VALUES ('suite_export_info', 'attributes')")
        for k, v in info.items():
            con.execute('INSERT INTO suite_export_info ("key", "value") VALUES (?, ?)', (k, v))
    con.commit()
    con.close()
    return path
