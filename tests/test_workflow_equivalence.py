"""P0-4 snapshot tolerance comparison."""

from __future__ import annotations

import unittest

from backend.workflow.equivalence import ComparisonProtocolV1, compare_snapshots
from tests.test_workflow_metrology import trench


def protocol(tolerance: float) -> ComparisonProtocolV1:
    return ComparisonProtocolV1(
        schemaVersion=1,
        protocolId="protocol_cd",
        version=1,
        pointTolerance=tolerance,
    )


class EquivalenceTests(unittest.TestCase):
    def test_close_snapshots_are_established(self):
        left = trench()
        right = trench()
        right["points"] = [[x + 0.01, h, z] for x, h, z in right["points"]]
        result = compare_snapshots(protocol(0.02), left, right)
        self.assertEqual(result.status, "established")
        self.assertLessEqual(result.maxPointDelta, 0.02)

    def test_large_delta_fails(self):
        left = trench()
        right = trench()
        right["points"] = [[x + 1.0, h, z] for x, h, z in right["points"]]
        result = compare_snapshots(protocol(0.02), left, right)
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.reasons)

    def test_connectivity_axis_and_unit_mismatch_fail(self):
        for mutate in (
            lambda data: data["lines"].reverse(),
            lambda data: data["axisMap"].update({"h": 2}),
            lambda data: data.update({"coordinateUnit": "nm"}),
        ):
            left, right = trench(), trench()
            mutate(right)
            result = compare_snapshots(protocol(0.02), left, right)
            self.assertEqual(result.status, "failed")

    def test_material_mismatch_fails(self):
        left = trench()
        right = trench()
        right["cellData"]["materialIds"][0] = 30
        result = compare_snapshots(protocol(0.02), left, right)
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.materialMismatch)


if __name__ == "__main__":
    unittest.main()
