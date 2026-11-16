"""Cross-engine probe and normalisation contracts (pure, no engines needed)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engines import normalize, probe, section  # noqa: E402


class LatticeTests(unittest.TestCase):
    def test_lattice_covers_the_declared_box(self):
        lat = probe.build_lattice(0.14, 0.288, 0.3, 0.4, 3000)
        xs = [p[0] for p in lat.points]
        ys = [p[1] for p in lat.points]
        zs = [p[2] for p in lat.points]
        self.assertGreaterEqual(min(xs), 0.0)
        self.assertLessEqual(max(xs), 0.14)
        self.assertGreaterEqual(min(ys), 0.0)
        self.assertLessEqual(max(ys), 0.288)
        self.assertGreaterEqual(min(zs), -0.3)
        self.assertLessEqual(max(zs), 0.4)
        self.assertLessEqual(len(lat.points), 12000)

    def test_lattice_sizes_are_monotone_in_the_budget(self):
        small = probe.build_lattice(0.14, 0.288, 0.3, 0.4, 500)
        large = probe.build_lattice(0.14, 0.288, 0.3, 0.4, 5000)
        self.assertLess(len(small.points), len(large.points))
        self.assertEqual(len(small.points), small.shape[0] * small.shape[1] * small.shape[2])

    def test_points_do_not_sit_on_the_domain_edges(self):
        lat = probe.build_lattice(0.1, 0.1, 0.1, 0.1, 1000)
        for x, y, z in lat.points:
            self.assertNotEqual(x, 0.0)
            self.assertNotEqual(y, 0.0)


class OccupancyMetricTests(unittest.TestCase):
    POINTS = [[0.5, 0.5, -0.1], [0.5, 0.5, 0.05], [0.5, 0.5, 0.2], [0.5, 0.5, 0.3]]

    def test_identical_labels_agree_completely(self):
        labels = ["Si", "SiO2", "void", "void"]
        metrics = probe.occupancy_metrics(self.POINTS, labels, labels)
        self.assertEqual(metrics["status"], "measured")
        self.assertEqual(metrics["agreement"], 1.0)
        self.assertEqual(metrics["mismatchCount"], 0)
        self.assertEqual(metrics["iou"], {"Si": 1.0, "SiO2": 1.0})

    def test_one_mismatch_is_counted_and_explained(self):
        left = ["Si", "SiO2", "void", "void"]
        right = ["Si", "void", "void", "void"]
        metrics = probe.occupancy_metrics(self.POINTS, left, right)
        self.assertAlmostEqual(metrics["agreement"], 0.75)
        self.assertEqual(metrics["mismatchCount"], 1)
        example = metrics["mismatchExamples"][0]
        self.assertEqual(example["point"], [0.5, 0.5, 0.05])
        self.assertEqual(example["viennaps"], "SiO2")
        self.assertEqual(example["spx"], "void")

    def test_missing_material_has_zero_iou(self):
        left = ["Si", "SiO2", "void", "void"]
        right = ["Si", "Si", "void", "void"]
        metrics = probe.occupancy_metrics(self.POINTS, left, right)
        self.assertEqual(metrics["iou"]["SiO2"], 0.0)
        self.assertEqual(metrics["iou"]["Si"], 0.5)

    def test_length_mismatch_is_invalid_not_zero(self):
        metrics = probe.occupancy_metrics(self.POINTS, ["Si"], ["Si"])
        self.assertEqual(metrics["status"], "invalid")

    def test_top_height_is_per_engine(self):
        left = ["Si", "SiO2", "void", "void"]
        right = ["Si", "Si", "void", "void"]
        metrics = probe.occupancy_metrics(self.POINTS, left, right)
        self.assertEqual(metrics["topHeightUm"]["viennaps"]["SiO2"], 0.05)
        self.assertEqual(metrics["topHeightUm"]["spx"]["SiO2"], None)


class SectionMetricTests(unittest.TestCase):
    COLUMNS = [[0.01, 0.1], [0.02, 0.1], [0.03, 0.1]]

    def test_identical_sections_have_zero_error(self):
        left = {"Si": [0.0, 0.0, 0.0], "SiO2": [0.1, 0.1, 0.1]}
        metrics = section.section_metrics(self.COLUMNS, ["Si", "SiO2"], left, left)
        self.assertEqual(metrics["status"], "measured")
        self.assertEqual(metrics["materials"]["SiO2"]["meanAbsUm"], 0.0)
        self.assertEqual(metrics["materials"]["SiO2"]["maxAbsUm"], 0.0)
        self.assertEqual(metrics["worstAbsUm"], 0.0)

    def test_known_offset_is_reported_as_mean_max_and_l2(self):
        left = {"SiO2": [0.1, 0.1, 0.1]}
        right = {"SiO2": [0.09, 0.11, 0.1]}
        metrics = section.section_metrics(self.COLUMNS, ["SiO2"], left, right)
        row = metrics["materials"]["SiO2"]
        self.assertAlmostEqual(row["meanAbsUm"], 0.006667, places=6)
        self.assertAlmostEqual(row["maxAbsUm"], 0.01, places=6)
        self.assertAlmostEqual(row["l2Um"], 0.008165, places=6)

    def test_unresolved_column_is_counted_not_zero(self):
        left = {"Si": [0.0, None, 0.0]}
        right = {"Si": [0.0, 0.0, 0.0]}
        metrics = section.section_metrics(self.COLUMNS, ["Si"], left, right)
        row = metrics["materials"]["Si"]
        self.assertEqual(row["resolved"], 2)
        self.assertEqual(row["missingColumns"], 1)
        self.assertEqual(row["meanAbsUm"], 0.0)

    def test_material_missing_on_both_sides_is_not_resolved(self):
        metrics = section.section_metrics(self.COLUMNS, ["SiO2"], {"SiO2": [None, None, None]},
                                          {"SiO2": [None, None, None]})
        self.assertEqual(metrics["status"], "not_resolved")
        self.assertEqual(metrics["materials"]["SiO2"]["status"], "not_resolved")

    def test_length_mismatch_is_reported(self):
        metrics = section.section_metrics(self.COLUMNS, ["Si"], {"Si": [0.0]}, {"Si": [0.0, 0.0]})
        self.assertEqual(metrics["materials"]["Si"]["status"], "length_mismatch")

    def test_heights_are_kept_for_the_ui(self):
        left = {"Si": [0.0, 0.0, 0.0]}
        metrics = section.section_metrics(self.COLUMNS, ["Si"], left, left)
        self.assertEqual(metrics["heights"]["viennaps"]["Si"], [0.0, 0.0, 0.0])


class NormalizeTests(unittest.TestCase):
    def test_spx_names_map_to_the_workbench_catalogue(self):
        self.assertEqual(normalize.canonical("Silicon", "spx"), "Si")
        self.assertEqual(normalize.canonical("Oxide", "spx"), "SiO2")
        self.assertEqual(normalize.canonical("Nitride", "spx"), "Si3N4")
        self.assertEqual(normalize.canonical("Photoresist", "spx"), "PMMA")

    def test_void_aliases_are_one_label(self):
        for raw in ("Gas", "None", "", "unknown", None):
            self.assertEqual(normalize.canonical(raw, "spx"), normalize.VOID)
        self.assertEqual(normalize.canonical("gas", "viennaps"), normalize.VOID)

    def test_workbench_names_pass_through(self):
        self.assertEqual(normalize.canonical("SiO2", "viennaps"), "SiO2")
        self.assertEqual(normalize.to_spx("SiO2"), "Oxide")
        self.assertEqual(normalize.to_spx("unknown-material"), "unknown-material")

    def test_colours_come_from_the_table(self):
        self.assertEqual(normalize.color("SiO2"), "#00e5ff")
        self.assertEqual(normalize.color("not-a-material"), "#888888")


if __name__ == "__main__":
    unittest.main()
