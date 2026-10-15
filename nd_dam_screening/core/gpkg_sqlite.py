"""0.29.0: make plain-sqlite3 writes to a GeoPackage safe in a real QGIS.

A GeoPackage that QGIS or GDAL wrote carries an rtree spatial index whose triggers call the SQL
function ST_IsEmpty. That function is registered by GDAL's SQLite dialect, not stored in the
file, so a plain Python `sqlite3` connection does not have it, and any INSERT / UPDATE / DELETE on
an indexed feature table raises "no such function: ST_IsEmpty". The Screening's merge (tool 18)
and the Review tab's "Move breach point" write with plain sqlite3, so on every real work folder
they failed; only the tests passed, because their GeoPackages were hand-written with no index.

`disable_rtree` removes the rtree index (its virtual table, its triggers and its gpkg_extensions
rows) before such a write. The feature table and its data are untouched and the file stays a
valid GeoPackage; it simply has no spatial index on that layer afterwards, which QGIS rebuilds if
it needs one. This keeps these modules pure Python (no GDAL import) and works the same in a real
QGIS and in the tests.
"""


def _geom_columns(con, table):
    try:
        rows = con.execute("SELECT column_name FROM gpkg_geometry_columns WHERE table_name=?",
                           (table,)).fetchall()
    except Exception:
        return []
    return [r[0] for r in rows if r and r[0]]


def disable_rtree(con, table):
    """Drop the rtree spatial index of `table` (triggers, virtual table, extension rows) so a
    plain-sqlite3 write to the table does not fire ST_IsEmpty. A no-op when there is no index.
    Returns True when an index was removed. The caller commits."""
    removed = False
    for gcol in _geom_columns(con, table):
        base = "rtree_{}_{}".format(table, gcol)
        trigs = con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
            (base + "_%",)).fetchall()
        for (name,) in trigs:
            con.execute('DROP TRIGGER IF EXISTS "{}"'.format(name))
            removed = True
        if con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (base,)).fetchone():
            con.execute('DROP TABLE IF EXISTS "{}"'.format(base))
            removed = True
        try:
            con.execute("DELETE FROM gpkg_extensions WHERE table_name=? AND column_name=? "
                        "AND extension_name='gpkg_rtree_index'", (table, gcol))
        except Exception:
            pass                            # no gpkg_extensions table: nothing to clean
    return removed
