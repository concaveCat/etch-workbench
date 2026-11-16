"""Layout reader tests: GDS always, OASIS only when gdstk is installed."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engines import layout  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "finfet22_workbench.gds"


def _gdstk_available() -> bool:
    try:
        __import__("gdstk")
        return True
    except ModuleNotFoundError:
        return False


class GdsLayoutTests(unittest.TestCase):
    def test_layers_and_extent_are_reported_in_um(self):
        summary = layout.summarise(FIXTURE)
        self.assertEqual(summary.source_format, "gds")
        self.assertEqual(summary.cells, ["FINFET22"])
        self.assertEqual([layer.layer for layer in summary.layers], [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertTrue(all(layer.datatype == 0 for layer in summary.layers))
        self.assertTrue(all(layer.polygons > 0 for layer in summary.layers))
        # The fixture is a 1 x 1 um cell centred on the origin.
        assert summary.extent_um is not None
        self.assertAlmostEqual(summary.extent_um[0], -0.5, places=6)
        self.assertAlmostEqual(summary.extent_um[2], 0.5, places=6)

    def test_layer_numbers_are_stable(self):
        summary = layout.summarise(FIXTURE)
        self.assertEqual(summary.layer_numbers(), [(n, 0) for n in range(1, 9)])

    def test_missing_file_is_an_error_not_empty_geometry(self):
        with self.assertRaises((OSError, layout.LayoutError, ValueError)):
            layout.summarise(REPO_ROOT / "tests" / "fixtures" / "does-not-exist.gds")


@unittest.skipUnless(_gdstk_available(), "OASIS support needs gdstk")
class OasisLayoutTests(unittest.TestCase):
    OASIS = Path("/root/SPX/spx/V-2024.03/linux64/examples/Memory1_Layout.oasis")

    @unittest.skipUnless(OASIS.is_file(), "the SPX example layout is not installed")
    def test_oasis_and_converted_gds_agree(self):
        import tempfile

        source = layout.summarise(self.OASIS)
        self.assertEqual(source.source_format, "oasis")
        self.assertEqual([layer.layer for layer in source.layers], [9, 11, 15, 17, 19, 21])
        with tempfile.TemporaryDirectory() as tmp:
            target = layout.to_gds(self.OASIS, Path(tmp) / "memory1.gds")
            converted = layout.summarise(target)
        self.assertEqual(converted.layer_numbers(), source.layer_numbers())
        for left, right in zip(source.layers, converted.layers):
            self.assertEqual(left.polygons, right.polygons)
            assert left.bounds_um and right.bounds_um
            for a, b in zip(left.bounds_um, right.bounds_um):
                self.assertAlmostEqual(a, b, places=5)
        assert source.extent_um and converted.extent_um
        for a, b in zip(source.extent_um, converted.extent_um):
            self.assertAlmostEqual(a, b, places=5)


if __name__ == "__main__":
    unittest.main()
