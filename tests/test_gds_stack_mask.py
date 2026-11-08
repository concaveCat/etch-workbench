"""Physics acceptance for the product GDS stack-mask helper.

Each case runs ``tests/fixtures/gds_stack_mask_probe.py`` (which imports the
shipped ``worker.gds_mask.build_aligned_gds_mask``) in its own supervised process.
The parent checks geometry, material and etch-rate invariants from the JSON
report; a clean exit code alone is never treated as success.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
PROBE = HERE / "fixtures" / "gds_stack_mask_probe.py"
GDS_FIXTURE = ROOT / "docs" / "gds-mask-diagnosis" / "fixtures" / "buf4-selected.gds"
EVIDENCE = ROOT / "docs" / "gds-mask-diagnosis" / "product-acceptance"
EMPTY_ERROR = "GDS 掩膜在当前计算域内为空，请调整版图位置、保留区域或计算域。"
SAMPLE_TOLERANCE = 1e-5

# (name, source, boundary, retained, delta, duration, extent, expected heights, expect empty)
CASES = [
    ("box-reflective-inside", "box", "reflective", "inside", 0.1, 0.2, 5.0,
     {"0.0,0.0": 5.0, "-1.5,1.0": 4.96}, False),
    ("box-reflective-outside", "box", "reflective", "outside", 0.1, 0.2, 5.0,
     {"0.0,0.0": 4.96, "-1.5,1.0": 5.0}, False),
    ("box-reflective-inside-nondivisible", "box", "reflective", "inside", 0.07, 0.2, 5.07,
     {"0.0,0.0": 5.0, "-1.5,1.0": 4.96}, False),
    ("ring-reflective-inside", "ring", "reflective", "inside", 0.1, 0.2, 5.0,
     {"0.0,0.0": 4.96, "0.75,0.1": 5.0}, False),
    ("ring-reflective-outside", "ring", "reflective", "outside", 0.1, 0.2, 5.0,
     {"0.0,0.0": 5.0, "0.75,0.1": 4.96}, False),
    ("full-reflective-inside", "full", "reflective", "inside", 0.1, 0.2, 5.0,
     {"0.0,0.0": 5.0, "-1.5,1.0": 5.0}, False),
    ("full-reflective-outside", "full", "reflective", "outside", 0.1, 0.2, 5.0, {}, True),
    ("outside-reflective-inside", "outside", "reflective", "inside", 0.1, 0.2, 5.0, {}, True),
    ("outside-reflective-outside", "outside", "reflective", "outside", 0.1, 0.2, 5.0,
     {"0.0,0.0": 5.0}, False),
    ("crossing-reflective-inside", "crossing", "reflective", "inside", 0.1, 0.2, 5.0,
     {"2.35,0.1": 5.0, "-2.35,0.1": 4.96}, False),
    ("crossing-periodic-inside", "crossing", "periodic", "inside", 0.1, 0.2, 5.0,
     {"2.35,0.1": 5.0, "-2.35,0.1": 5.0, "0.0,0.0": 4.96}, False),
    ("outside-periodic-inside", "outside", "periodic", "inside", 0.1, 0.2, 5.0,
     {"-1.6,0.1": 5.0, "0.0,0.0": 4.96}, False),
    ("gds-reflective-inside-01", "gds", "reflective", "inside", 0.1, 0.2, 5.0, {}, False),
    ("gds-reflective-inside-003", "gds", "reflective", "inside", 0.03, 0.2, 5.0, {}, False),
    ("gds-reflective-inside-01-short", "gds", "reflective", "inside", 0.1, 0.01, 5.0, {}, False),
    ("gds-reflective-outside-01", "gds", "reflective", "outside", 0.1, 0.2, 5.0, {}, False),
]


class GdsStackMaskPhysics(unittest.TestCase):
    maxDiff = None

    def run_case(self, case):
        (name, source, boundary, retained, delta, duration, extent,
         expected_heights, expect_empty) = case
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        report_path = EVIDENCE / f"{name}.json"
        guard_path = EVIDENCE / f"{name}.guard.json"
        log_path = EVIDENCE / f"{name}.log"
        for stale in (report_path, guard_path, log_path):
            stale.unlink(missing_ok=True)
        command = [
            str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/bounded_child.py"),
            "--cwd", str(ROOT), "--record", str(guard_path), "--log-path", str(log_path),
            "--timeout-seconds", "45", "--max-rss-mib", "2048", "--",
            str(ROOT / ".venv/bin/python"), str(PROBE), source, str(GDS_FIXTURE),
            str(report_path), "--boundary", boundary, "--retained", retained,
            "--delta", str(delta), "--duration", str(duration), "--extent", str(extent),
        ]
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=55)
        guard = json.loads(guard_path.read_text()) if guard_path.exists() else {}
        self.assertEqual(
            run.returncode, 0,
            f"{name}: probe failed (returnCode={run.returncode}, "
            f"termination={guard.get('terminationReason')}): {run.stderr[-500:]}")
        self.assertNotIn(guard.get("terminationReason"), {"timeout", "rss_limit", "orphaned_child"})
        self.assertTrue(report_path.exists(), f"{name}: missing native report")
        return json.loads(report_path.read_text())

    def check_materials(self, name, report):
        after = report["after"]
        self.assertEqual(report["setupBefore"], report["setupAfter"],
                         f"{name}: simulation domain/boundary/gridDelta changed")
        self.assertFalse(any(value == 0 for value in after["levelSetPoints"]),
                         f"{name}: a material level set vanished: {after['levelSetPoints']}")
        for material in ("10", "30", "0"):
            self.assertGreater(after["materials"].get(material, {}).get("triangles", 0), 0,
                               f"{name}: material {material} has no surface")
        self.assertGreater(after["points"], 0)

    def check_heights(self, name, report, expected):
        for point, target in expected.items():
            height = report["after"]["filmHeights"].get(point)
            self.assertIsNotNone(height, f"{name}: no SiO2 film at {point}")
            self.assertLessEqual(abs(height - target), SAMPLE_TOLERANCE,
                                 f"{name}: film at {point} = {height}, expected {target}")

    def check_diagnostics(self, name, report, retained):
        diagnostics = report["diagnostics"]
        self.assertEqual(diagnostics["gridSource"], "simulation-domain", name)
        self.assertIs(diagnostics["wrapLowerLevelSet"], True, name)
        self.assertEqual(diagnostics["retainedRegion"], retained, name)
        self.assertIn(diagnostics["construction"],
                      {"native-extrude-capped-surface-regrid-union-v1",
                       "native-layer-to-levelset-regrid-union-v1"}, name)
        if diagnostics["construction"] == "native-layer-to-levelset-regrid-union-v1":
            self.assertGreater(diagnostics["nativeBodyPointCount"], 0, name)
        else:
            for key in ("extrudedPointCount",):
                self.assertGreater(diagnostics[key], 0, f"{name}: {key} must be positive")
        for key in ("alignedMaskPointCount", "sourceSurfacePointCount",
                    "sourceSurfaceTriangleCount"):
            self.assertGreater(diagnostics[key], 0, f"{name}: {key} must be positive")
        self.assertEqual(diagnostics["alignedMaskPointCount"], report["maskPoints"], name)

    def test_cases(self):
        for case in CASES:
            name = case[0]
            with self.subTest(case=name):
                report = self.run_case(case)
                if case[8]:
                    self.assertEqual(report.get("emptyError"), EMPTY_ERROR,
                                     f"{name}: expected the explicit empty-mask error")
                    self.assertNotIn("after", report, name)
                    continue
                self.assertNotIn("emptyError", report, f"{name}: unexpected empty mask")
                self.check_diagnostics(name, report, case[3])
                self.check_materials(name, report)
                self.check_heights(name, report, case[7])


if __name__ == "__main__":
    unittest.main()
