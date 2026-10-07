"""A proposed embankment on a synthetic valley. No QGIS.

Run: python3 tests/test_proposed.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import proposed as pr  # noqa: E402

FT = pr.FT


def bowl(x0=20.0, y_top=200.0, cell=2.0, n=90):
    """Ground in metres. A bowl centred at (100, 100); y decreases down the rows."""
    gt = (x0, cell, 0.0, y_top, 0.0, -cell)
    xs = x0 + (np.arange(n) + 0.5) * cell
    ys = y_top - (np.arange(n) + 0.5) * cell
    X, Y = np.meshgrid(xs, ys)
    z = 100.0 + ((Y - 100.0) / 30.0) ** 2 * 8.0 + ((X - 100.0) / 40.0) ** 2 * 6.0
    return z, gt, X, Y


def test_contained():
    z, gt, X, Y = bowl()
    raw = z.copy()
    line = np.array([[100.0, 40.0], [100.0, 160.0]])
    emb, stor, text, conf = pr.build(
        z, gt, line, crest_m=104.0, crest_width_m=12 * FT, us_h=3.0, ds_h=3.0,
        upstream="left", upstream_given=True, steps=24)
    assert np.array_equal(z, raw, equal_nan=True), "the source DEM was modified"
    assert abs(emb["bed_m"] - 100.0) < 0.15, emb["bed_m"]
    assert abs(emb["height_m"] - 4.0) < 0.2, emb["height_m"]
    assert abs(emb["breach_xy"][0] - 100.0) < 2.0
    assert abs(emb["breach_xy"][1] - 100.0) < 4.0, emb["breach_xy"]
    assert emb["crest_len_m"] > 30.0
    assert stor["rim_kind"] == "contained", (stor["rim_kind"], stor["rim_m"])
    assert stor["storage_crest_m3"] > 1000.0
    assert stor["pool_m"] > emb["bed_m"] and stor["pool_m"] < emb["bed_m"] + 0.2
    assert text.startswith(pr.PHRASE)
    assert conf == "high"
    # the crest cells sit at the design elevation; the faces fall to natural ground
    on = emb["raised"] & (np.abs(emb["d_up"]) <= 0.5 * emb["crest_width_m"] + 1e-6)
    assert on.any() and np.allclose(emb["dem"][on], 104.0)
    assert np.all(emb["dem"][emb["raised"]] >= raw[emb["raised"]] - 1e-6)
    again = pr.insert_embankment(
        emb["dem"], gt, line, 104.0, 12 * FT, 3.0, 3.0, "left", measure=False)
    assert np.nanmax(np.abs(again["dem"] - emb["dem"])) < 1e-6
    # a hillside higher than the crest, next to the breach, must not fail the check
    spiked = emb["dem"].copy()
    spiked[0, 0] = 140.0
    crest_ft, err = pr.crest_check(spiked, X, Y, emb["breach_xy"], 104.0, 12 * FT)
    assert err is None and abs(crest_ft - 104.0 / FT) < 0.01, (crest_ft, err)
    _ft, err2 = pr.crest_check(raw, X, Y, emb["breach_xy"], 104.0, 12 * FT)
    assert err2 and "not in the grid" in err2
    spec = pr.json_safe_spec(emb, stor, line, [[-100.5, 47.0], [-100.4, 47.1]], "PROP-1")
    assert spec["phrase"] == pr.PHRASE and spec["crs"] == "EPSG:5070"
    assert spec["rim_kind"] == "contained" and spec["line_xy"][0][0] == 100.0
    print("contained ok", round(emb["height_m"], 2), "m,",
          round(stor["storage_crest_m3"] / pr.AF_M3, 1), "ac-ft")


def test_saddle():
    z, gt, _X, _Y = bowl()
    line = np.array([[100.0, 90.0], [100.0, 110.0]])
    emb, stor, text, conf = pr.build(
        z, gt, line, 104.0, 12 * FT, 3.0, 3.0, upstream="left", upstream_given=False)
    assert stor["rim_kind"] == "saddle", (stor["rim_kind"], stor["rim_m"])
    assert stor["rim_m"] < 104.0 - 0.5, stor["rim_m"]
    assert stor["holdable_m3"] < stor["storage_crest_m3"]
    assert conf == "low"
    assert "pool leaves the basin" in text
    assert "left of the drawn direction" in text
    print("saddle ok", round(stor["rim_m"], 2), "m rim, holdable",
          round(stor["holdable_m3"] / pr.AF_M3, 2), "ac-ft")


def test_off_grid_does_not_raise_when_unmeasured():
    z, gt, _X, _Y = bowl()
    line = np.array([[10000.0, 0.0], [10000.0, 50.0]])
    emb = pr.insert_embankment(z, gt, line, 104.0, 4.0, 3.0, 3.0, "left", measure=False)
    assert not emb["raised"].any()


if __name__ == "__main__":
    test_contained()
    test_saddle()
    test_off_grid_does_not_raise_when_unmeasured()
    print("proposed.py ok")
