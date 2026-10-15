"""Tests for core/comparison.py (Item 8: Runner vs Screener comparison sheet)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from core.comparison import (
    read_runner_manifest, build_row, write_comparison_html,
    FT_PER_M, CFS_PER_M3S, AC_PER_HA,
)

SAMPLE_MANIFEST = {
    "manifest_version": 1,
    "dam": {
        "dam_id": "ND-0001",
        "dam_height_ft": 25.0,
        "storage_top_acft": 500.0,
        "top_of_dam_ft": 2193.9,
        "name": "Raleigh Dam",
    },
    "hydrograph": {
        "method": "ND Method",
        "peak_cfs": 51407.0,
        "time_to_peak_hr": 0.5,
        "duration_hr": 6.0,
        "volume_acft": 450.0,
    },
    "settings": {
        "cell_size_m": 50.0,
        "lisflood_solver": "acc",
    },
    "extra": {
        "final_model": "lisflood",
        "lisflood_solver": "acc",
    },
}

SAMPLE_ATTRS = {
    "dam_id": "ND-0001",
    "name": "Raleigh Dam",
    "hz_class": "High",
    "qp_m3s": 1755.0,  # ~61,967 cfs
    "est_crest_m": 669.3,  # ~2195.9 ft
    "area_ha": 22.5,  # ~55.6 ac
    "max_depth_m": 3.2,  # ~10.5 ft
    "n_bldg_wet": 12,
    "n_bldg_lethal": 8,
    "n_road_wet": 3,
    "grid_m": 50.0,
    "dem_m": 3.0,
}

SAMPLE_RES = {
    "rn_class": "High",
    "rn_agree": "agree",
    "rn_status": "done",
    "rn_border": "no",
    "rn_gaps": "",
    "rn_inputs": "sent: screening class High",
}


class TestReadRunnerManifest(unittest.TestCase):
    def test_reads_fields(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "run_manifest.json")
            with open(p, "w") as f:
                json.dump(SAMPLE_MANIFEST, f)
            m = read_runner_manifest(d)
        self.assertAlmostEqual(m["rn_top_ft"], 2193.9)
        self.assertAlmostEqual(m["rn_peak_cfs"], 51407.0)
        self.assertEqual(m["rn_method"], "ND Method")
        self.assertAlmostEqual(m["rn_cell_m"], 50.0)
        self.assertEqual(m["rn_solver"], "acc")
        self.assertAlmostEqual(m["rn_height_ft"], 25.0)
        self.assertAlmostEqual(m["rn_storage_acft"], 500.0)

    def test_missing_folder(self):
        m = read_runner_manifest("/nonexistent/path")
        self.assertEqual(m, {})

    def test_missing_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            m = read_runner_manifest(d)
        self.assertEqual(m, {})

    def test_corrupt_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "run_manifest.json")
            with open(p, "w") as f:
                f.write("not json")
            m = read_runner_manifest(d)
        self.assertEqual(m, {})


class TestBuildRow(unittest.TestCase):
    def _row(self, attrs=None, res=None, manifest=None):
        with tempfile.TemporaryDirectory() as d:
            mpath = os.path.join(d, "run_manifest.json")
            with open(mpath, "w") as f:
                json.dump(SAMPLE_MANIFEST, f)
            man = read_runner_manifest(d)
        return build_row("ND-0001", attrs or SAMPLE_ATTRS,
                         res or SAMPLE_RES, manifest if manifest is not None else man)

    def test_classes(self):
        r = self._row()
        self.assertEqual(r["scr_class"], "High")
        self.assertEqual(r["rn_class"], "High")
        self.assertEqual(r["rn_agree"], "agree")

    def test_peak_flow_conversion(self):
        r = self._row()
        # screener: 1755 m3/s * 35.3147 ≈ 61,977 cfs
        self.assertAlmostEqual(r["scr_peak_cfs"], 1755.0 * CFS_PER_M3S, delta=1)
        self.assertAlmostEqual(r["rn_peak_cfs"], 51407.0, delta=1)
        self.assertIsNotNone(r["peak_ratio"])
        self.assertAlmostEqual(r["peak_ratio"], 51407.0 / (1755.0 * CFS_PER_M3S), places=3)

    def test_top_of_dam_delta(self):
        r = self._row()
        # screener: 669.3 m * 3.28084 ≈ 2195.9 ft; runner: 2193.9 ft
        scr_ft = 669.3 * FT_PER_M
        self.assertAlmostEqual(r["scr_crest_ft"], scr_ft, delta=0.1)
        self.assertAlmostEqual(r["rn_top_ft"], 2193.9, delta=0.01)
        self.assertAlmostEqual(r["delta_crest_ft"], 2193.9 - scr_ft, delta=0.1)

    def test_screener_flood_fields(self):
        r = self._row()
        self.assertAlmostEqual(r["scr_area_ac"], 22.5 * AC_PER_HA, delta=0.1)
        self.assertAlmostEqual(r["scr_depth_ft"], 3.2 * FT_PER_M, delta=0.01)
        self.assertEqual(r["scr_bldg_wet"], 12)
        self.assertEqual(r["scr_bldg_let"], 8)
        self.assertEqual(r["scr_road_wet"], 3)

    def test_model_settings(self):
        r = self._row()
        self.assertEqual(r["scr_grid_m"], 50.0)
        self.assertAlmostEqual(r["rn_cell_m"], 50.0)
        self.assertEqual(r["rn_method"], "ND Method")
        self.assertEqual(r["rn_solver"], "acc")

    def test_empty_manifest(self):
        r = self._row(manifest={})
        self.assertIsNone(r["rn_top_ft"])
        self.assertIsNone(r["rn_peak_cfs"])
        self.assertIsNone(r["delta_crest_ft"])
        self.assertIsNone(r["peak_ratio"])

    def test_missing_screener_fields(self):
        r = self._row(attrs={"dam_id": "X", "hz_class": "Medium"})
        self.assertEqual(r["scr_class"], "Medium")
        self.assertIsNone(r["scr_peak_cfs"])
        self.assertIsNone(r["scr_crest_ft"])


class TestWriteComparisonHtml(unittest.TestCase):
    def test_writes_html(self):
        with tempfile.TemporaryDirectory() as d:
            mpath = os.path.join(d, "run_manifest.json")
            with open(mpath, "w") as f:
                json.dump(SAMPLE_MANIFEST, f)
            man = read_runner_manifest(d)
            row = build_row("ND-0001", SAMPLE_ATTRS, SAMPLE_RES, man)
            out = os.path.join(d, "comparison.html")
            write_comparison_html(out, [row])
            self.assertTrue(os.path.isfile(out))
            html = open(out).read()
            self.assertIn("ND-0001", html)
            self.assertIn("High", html)
            self.assertIn("Screener", html)
            self.assertIn("Runner", html)

    def test_empty_rows_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "comparison.html")
            write_comparison_html(out, [])
            self.assertFalse(os.path.isfile(out))

    def test_multi_dam(self):
        with tempfile.TemporaryDirectory() as d:
            mpath = os.path.join(d, "run_manifest.json")
            with open(mpath, "w") as f:
                json.dump(SAMPLE_MANIFEST, f)
            man = read_runner_manifest(d)
            rows = [build_row("ND-0001", SAMPLE_ATTRS, SAMPLE_RES, man),
                    build_row("ND-0002", {**SAMPLE_ATTRS, "dam_id": "ND-0002",
                                          "hz_class": "Medium"},
                              {**SAMPLE_RES, "rn_class": "High", "rn_agree": "runner higher"},
                              man)]
            out = os.path.join(d, "comparison.html")
            write_comparison_html(out, rows)
            html = open(out).read()
            self.assertIn("ND-0001", html)
            self.assertIn("ND-0002", html)
            self.assertIn("runner higher", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
