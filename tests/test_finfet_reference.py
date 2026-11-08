"""Relationship between the original nm reference and the project-frame twin."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "docs" / "finfet-example"
NM_SCRIPT = FIXTURE / "reference" / "FinFET.py"
UM_SCRIPT = FIXTURE / "reference" / "FinFET_um.py"
NM_EXPECTED = FIXTURE / "expected.json"
UM_EXPECTED = FIXTURE / "expected-um.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FinFETReferenceFramesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.nm = json.loads(NM_EXPECTED.read_text(encoding="utf-8"))
        cls.um = json.loads(UM_EXPECTED.read_text(encoding="utf-8"))

    def test_reference_script_hashes_are_the_committed_sources(self) -> None:
        self.assertEqual(self.nm["referenceScript"]["sha256"], sha256(NM_SCRIPT))
        self.assertEqual(self.um["referenceScript"]["sha256"], sha256(UM_SCRIPT))

    def test_both_frames_have_26_surfaces_and_a_final_volume(self) -> None:
        for label, expected in (("nm", self.nm), ("um", self.um)):
            with self.subTest(frame=label):
                self.assertEqual([row["index"] for row in expected["surfaces"]], list(range(26)))
                self.assertEqual(expected["volume"]["cellType"], "tetra")
                self.assertGreater(expected["volume"]["cellCount"], 0)
                self.assertEqual(len(expected["volume"]["digest"]), 64)

    def test_frames_have_the_same_topology_and_materials_through_gate_mask(self) -> None:
        for index in range(14):
            with self.subTest(surface=index):
                left = self.nm["surfaces"][index]
                right = self.um["surfaces"][index]
                self.assertEqual(right["pointCount"], left["pointCount"])
                self.assertEqual(right["triangleCount"], left["triangleCount"])
                self.assertEqual(right["materialMap"], left["materialMap"])
                # Digests are intentionally in each frame's native coordinates;
                # the nm project and the um twin are exact unit scalings here.
                self.assertNotEqual(right["digest"], left["digest"])

    def test_masked_gate_checkpoint_documents_the_native_scale_divergence(self) -> None:
        left = self.nm["surfaces"][14]
        right = self.um["surfaces"][14]
        self.assertEqual(right["pointCount"], left["pointCount"])
        self.assertEqual(right["materialMap"], left["materialMap"])
        self.assertNotEqual(right["digest"], left["digest"])
        self.assertNotEqual(self.um["volume"]["digest"], self.nm["volume"]["digest"])


if __name__ == "__main__":
    unittest.main()
