"""v3.7.0: a proposed embankment from dam.json is raised onto the grid the models read,
and the top-of-dam check looks for that crest rather than the highest hillside."""
import os
import tempfile

import numpy as np

from nd_breach_runner.core import conditioning as cond
from nd_breach_runner.core import workflow
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.proposed import PHRASE, crest_check
from nd_breach_runner.tests.registry import test

FT = 0.3048


def _valley():
    n, cell = 80, 2.0
    x0, y0 = 20.0, 180.0
    xs = x0 + (np.arange(n) + 0.5) * cell
    ys = y0 - (np.arange(n) + 0.5) * cell
    X, Y = np.meshgrid(xs, ys)
    z = 100.0 + ((Y - 100.0) / 30.0) ** 2 * 8.0 + ((X - 100.0) / 40.0) ** 2 * 6.0
    # a hillside higher than the design crest, inside the old 30 m check
    z[38, 42] = 130.0
    g = Grid(z, x0, y0, cell)
    return g, X, Y


def _spec():
    return {
        "phrase": PHRASE, "crs": "EPSG:5070",
        "line_xy": [[100.0, 40.0], [100.0, 160.0]],
        "line_lonlat": [[-100.5, 46.9], [-100.4, 47.1]],
        "crest_m": 104.0, "crest_width_m": 12 * FT,
        "us_slope_h": 3.0, "ds_slope_h": 3.0, "upstream": "left",
        "crest_len_m": 40.0, "rim_kind": "contained",
    }


@test
def proposed_embankment_is_inserted_and_idempotent():
    g, X, Y = _valley()
    raw = g.data.copy()
    ed = cond.Edits(embankments=[cond.EmbankmentInsert(_spec())])
    out, _init, counts = ed.apply(g)
    assert counts["embankments:proposed embankment"] > 0
    assert np.array_equal(g.data, raw)          # the grid passed in is not the output
    z = out.masked()
    breach = (100.0, 100.0)
    crest_ft, err = crest_check(z, X, Y, breach, 104.0, 12 * FT)
    assert err is None and abs(crest_ft - 104.0 / FT) < 0.02, (crest_ft, err)
    # the hillside spike is still there, and it is not what the check reports
    assert np.nanmax(z) >= 130.0
    again, _, counts2 = cond.Edits(embankments=[cond.EmbankmentInsert(_spec())]).apply(out)
    assert counts2["embankments:proposed embankment"] == 0
    assert np.nanmax(np.abs(again.masked() - z)) < 1e-4


@test
def proposed_record_sets_the_edit_and_the_crest_length():
    d = tempfile.mkdtemp(prefix="propdam_")
    from nd_breach_runner.damsuite import dam_record as dr
    rec = dr.DamRecord(d)
    rec.set_many({"dam_id": "PROP-1", "height_ft": 13.1, "top_of_dam_ft": 341.2,
                  "dam_length_ft": 131.2, "storage_acft": 1.2}, "ndscreen", "0.14.0", note=PHRASE)
    rec.extra["proposed_embankment"] = _spec()
    rec.save()
    from nd_breach_runner.core import breach as br
    dam = br.DamData(dam_id="PROP-1", dam_height_ft=13.1, storage_top_acft=1.2,
                     top_of_dam_ft=341.2)
    st = workflow.RunSettings()
    warnings = []
    workflow.attach_proposed(st, d, dam, warnings, lambda m: None)
    assert st.edits is not None and len(st.edits.embankments) == 1
    assert abs(dam.crest_length_ft - 40.0 / FT) < 0.01
    assert PHRASE in warnings
    # an ordinary run does not grow a proposed key
    plain = workflow.RunSettings()
    assert "proposed" not in plain.as_dict()
    assert "embankments" not in workflow.key_settings(workflow.RunSettings(edits=cond.Edits()))["edits"]
    os_cleanup = d
    import shutil
    shutil.rmtree(os_cleanup)
