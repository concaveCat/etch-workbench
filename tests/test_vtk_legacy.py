from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.vtk_legacy import read_legacy_vtk


FIXTURE = """# vtk DataFile Version 3.0
minimal independent test mesh
ASCII
DATASET UNSTRUCTURED_GRID
POINTS 2 float
0 0 0
1 0 0
CELLS 1 3
2 0 1
CELL_TYPES 1
3
CELL_DATA 1
SCALARS MaterialIds int
LOOKUP_TABLE default
10
"""


class VtkLegacyTests(unittest.TestCase):
    def test_reads_line_cells_and_original_material_id_array(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "one-line.vtk"
            path.write_text(FIXTURE, encoding="ascii")
            mesh = read_legacy_vtk(path)
            self.assertEqual(mesh["pointCount"], 2)
            self.assertEqual(mesh["cellTypes"], [3])
            self.assertEqual(mesh["cellData"]["MaterialIds"], [10.0])
            self.assertEqual(mesh["cells"], [[0, 1]])


if __name__ == "__main__":
    unittest.main()
