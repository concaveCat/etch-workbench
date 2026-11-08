"""Step-scoped material tables and sub-micron 3D lateral domains.

Two contracts are covered here:

* Material tables of a specialized model describe the stack that exists when
  that step runs, so a material introduced by an earlier enabled deposition
  must be selectable, while a material that only appears later must not.
* The 3D lateral domain floor must accept a real 0.48 x 0.50 um 22 nm cell
  without letting degenerate domains through. The 2D domain keeps its legacy
  1 um floor.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from backend.models import (  # noqa: E402
    MIN_DOMAIN_EXTENT_3D,
    Domain,
    Domain3D,
    parse_project,
)
from backend.validation import ProjectInputError, validate_project  # noqa: E402
from stage8_project_factory import make_project  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
FINFET_PROJECT = FIXTURES / "finfet22_project.json"
FINFET_GDS = FIXTURES / "finfet22_workbench.gds"


def deposit_step(step_id: str, material: str = "Si3N4") -> dict:
    return {
        "id": step_id,
        "name": f"{material} deposition",
        "enabled": True,
        "operation": "deposit",
        "model": "isotropic",
        "rateMagnitude": 0.05,
        "duration": 0.2,
        "depositMaterial": material,
    }


def project_without_nitride(model_id: str) -> dict:
    """A schema-7 project whose initial stack has no Si3N4."""
    project = make_project(model_id, 3)
    project["geometry"]["layers"] = [
        row for row in project["geometry"]["layers"] if row["material"] != "Si3N4"
    ]
    return project


class StepScopedMaterialTables(unittest.TestCase):
    def test_ion_beam_rate_table_accepts_material_from_earlier_deposition(self):
        project = project_without_nitride("ionBeam")
        table = project["steps"][0]["nativeParameters"]["materialPlaneWaferRateByMaterial"]
        table["Si3N4"] = 0.002
        project["steps"].insert(0, deposit_step("dep_sin_before"))
        parsed, _warnings = validate_project(project)
        self.assertEqual([step.model for step in parsed.steps], ["isotropic", "specialized"])

    def test_ion_beam_rate_table_rejects_material_introduced_later(self):
        project = project_without_nitride("ionBeam")
        project["steps"][0]["nativeParameters"]["materialPlaneWaferRateByMaterial"]["Si3N4"] = 0.002
        project["steps"].append(deposit_step("dep_sin_after"))
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project)
        issues = [issue for issue in ctx.exception.issues
                  if issue.path == "steps[0].nativeParameters.materialPlaneWaferRateByMaterial"]
        self.assertTrue(issues, [issue.path for issue in ctx.exception.issues])
        self.assertIn("Si3N4", issues[0].message)
        self.assertIn("earlier enabled deposition", issues[0].message)

    def test_disabled_earlier_deposition_does_not_introduce_a_material(self):
        project = project_without_nitride("ionBeam")
        project["steps"][0]["nativeParameters"]["materialPlaneWaferRateByMaterial"]["Si3N4"] = 0.002
        disabled = deposit_step("dep_sin_off")
        disabled["enabled"] = False
        project["steps"].insert(0, disabled)
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project)
        self.assertTrue(any(issue.path == "steps[1].nativeParameters.materialPlaneWaferRateByMaterial"
                            for issue in ctx.exception.issues))

    def test_disabled_draft_steps_keep_a_previewed_project_valid(self):
        # Disabling the SiN deposition and its IBE step must not invalidate the
        # project used for a preview: neither step runs, so the draft table is
        # only checked against the plan.
        project = project_without_nitride("ionBeam")
        project["steps"][0]["nativeParameters"]["materialPlaneWaferRateByMaterial"]["Si3N4"] = 0.002
        disabled = deposit_step("dep_sin_off")
        disabled["enabled"] = False
        project["steps"].insert(0, disabled)
        project["steps"][1]["enabled"] = False
        parsed, _warnings = validate_project(project, kind="preview")
        self.assertFalse(any(step.enabled for step in parsed.steps))

    def test_wet_etch_rate_table_covers_the_stack_at_that_step(self):
        project = project_without_nitride("wetEtch")
        table = project["steps"][0]["nativeParameters"]["materialRates"]
        table.pop("Si3N4", None)
        validate_project(project)

        project["steps"].insert(0, deposit_step("dep_sin_before"))
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project)
        messages = [issue.message for issue in ctx.exception.issues
                    if issue.path == "steps[1].nativeParameters.materialRates"]
        self.assertTrue(messages)
        self.assertIn("Si3N4", messages[0])

        project["steps"][1]["nativeParameters"]["materialRates"]["Si3N4"] = 0.02
        validate_project(project)

    def test_finfet22_project_with_sin_ion_beam_table_validates(self):
        project = json.loads(FINFET_PROJECT.read_text(encoding="utf-8"))
        # The 22 nm array is 0.48 x 0.50 um, and step 2 etches the SiN spacer
        # deposited by step 1, so its IBE table must carry a Si3N4 rate.
        project["domain"]["xExtent"] = 0.48
        project["domain"]["yExtent"] = 0.50
        project["steps"][1]["nativeParameters"]["materialPlaneWaferRateByMaterial"]["Si3N4"] = 0.002
        with patch("backend.storage.resolve_gds_asset", return_value=FINFET_GDS):
            parsed, warnings = validate_project(project)
        self.assertEqual(parsed.schemaVersion, 7)
        self.assertEqual(parsed.dimension, 3)
        self.assertEqual(warnings, [])


class ThreeDimensionalDomainFloor(unittest.TestCase):
    def test_sub_micron_cell_is_accepted(self):
        domain = Domain3D(gridDelta=0.004, xExtent=0.48, yExtent=0.50, boundaryXY="reflective")
        self.assertEqual((domain.xExtent, domain.yExtent), (0.48, 0.50))

        project = make_project("ionBeam", 3)
        project["domain"] = {"gridDelta": 0.004, "xExtent": 0.48, "yExtent": 0.50,
                             "boundaryXY": "reflective"}
        parsed, _warnings = validate_project(project)
        self.assertEqual((parsed.domain.xExtent, parsed.domain.yExtent), (0.48, 0.50))

    def test_degenerate_extents_are_rejected(self):
        for extent in (0.0, -0.5, MIN_DOMAIN_EXTENT_3D - 1e-6):
            with self.subTest(extent=extent):
                with self.assertRaises(ValueError):
                    Domain3D(gridDelta=0.004, xExtent=extent, yExtent=0.50, boundaryXY="reflective")

    def test_extent_must_still_span_two_grid_intervals(self):
        with self.assertRaises(ValueError):
            Domain3D(gridDelta=0.4, xExtent=0.5, yExtent=0.5, boundaryXY="reflective")

    def test_2d_domain_keeps_its_one_micrometre_floor(self):
        with self.assertRaises(ValueError):
            Domain(gridDelta=0.1, xExtent=0.48, xBoundary="reflective")
        Domain(gridDelta=0.1, xExtent=1.0, xBoundary="reflective")


class DomainBranchErrors(unittest.TestCase):
    def test_invalid_3d_domain_reports_only_the_3d_branch(self):
        project = make_project("ionBeam", 3)
        project["domain"]["xExtent"] = 0.05
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project)
        paths = [issue.path for issue in ctx.exception.issues]
        self.assertEqual(paths, ["domain.xExtent"])

    def test_invalid_2d_domain_reports_only_the_2d_branch(self):
        project = make_project("ionBeam", 2)
        project["domain"]["xExtent"] = 0.5
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project)
        paths = [issue.path for issue in ctx.exception.issues]
        self.assertEqual(paths, ["domain.xExtent"])

    def test_three_dimensional_payload_parses_with_a_small_domain(self):
        project = make_project("ionBeam", 3)
        project["domain"] = {"gridDelta": 0.004, "xExtent": 0.48, "yExtent": 0.50,
                             "boundaryXY": "reflective"}
        parsed = parse_project(copy.deepcopy(project))
        self.assertIsInstance(parsed.domain, Domain3D)
        self.assertEqual(parsed.model_dump(mode="json", exclude_none=True)["domain"],
                         {"gridDelta": 0.004, "xExtent": 0.48, "yExtent": 0.5,
                          "boundaryXY": "reflective"})


if __name__ == "__main__":
    unittest.main()
