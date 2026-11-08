"""Schema and catalog coverage for the FinFET acceptance fixture."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from backend.hashing import requested_input_hash
from backend.materials import material_catalog, referenced_material_names
from backend.models import Domain3D, InsertBoxStepV7, ParticleShapeStepV7, ProjectV7
from backend.validation import ProjectInputError, validate_project

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "docs" / "finfet-example" / "project.json"


def project_payload() -> dict:
    return json.loads(PROJECT.read_text(encoding="utf-8"))


class FinFETDomainTest(unittest.TestCase):
    def test_native_absolute_bounds_are_accepted_and_serialized(self) -> None:
        parsed = ProjectV7.model_validate(project_payload())
        self.assertIsInstance(parsed.domain, Domain3D)
        dumped = parsed.domain.model_dump(mode="json", exclude_none=True)
        self.assertEqual(dumped["bounds"], [0.0, 90.0, 0.0, 100.0, 0.0, 70.0])

    def test_unset_bounds_stay_out_of_serialization(self) -> None:
        domain = Domain3D(gridDelta=0.01, xExtent=0.5, yExtent=0.5, boundaryXY="reflective")
        self.assertNotIn("bounds", domain.model_dump(mode="json", exclude_none=True))
        payload = project_payload()
        payload.pop("bounds", None)
        payload["domain"].pop("bounds", None)
        parsed = ProjectV7.model_validate(payload)
        self.assertNotIn("bounds", parsed.model_dump(mode="json", exclude_none=True)["domain"])

    def test_bounds_must_match_extents_and_span_two_cells(self) -> None:
        with self.assertRaises(ValueError):
            Domain3D(gridDelta=0.01, xExtent=0.5, yExtent=0.5, boundaryXY="reflective",
                     bounds=[0.0, 0.4, 0.0, 0.5, 0.0, 0.1])
        with self.assertRaises(ValueError):
            Domain3D(gridDelta=0.01, xExtent=0.5, yExtent=0.5, boundaryXY="reflective",
                     bounds=[0.0, 0.5, 0.0, 0.5, 0.1, 0.0])
        with self.assertRaises(ValueError):
            Domain3D(gridDelta=0.01, xExtent=0.5, yExtent=0.5, boundaryXY="reflective",
                     bounds=[0.5, 0.0, 0.0, 0.5, 0.0, 0.1])

    def test_finfet_scale_domain_passes_validation(self) -> None:
        parsed, warnings = validate_project(project_payload(), kind="simulate")
        self.assertEqual(parsed.schemaVersion, 7)
        self.assertEqual(parsed.domain.xExtent, 90.0)
        self.assertEqual(parsed.domain.yExtent, 100.0)
        self.assertEqual(parsed.domain.gridDelta, 0.79)
        self.assertEqual(parsed.units.length, "nm")
        self.assertEqual(parsed.units.rate, "nm/s")
        self.assertEqual(len(parsed.steps), 25)
        self.assertTrue(warnings)  # particleShape warns by design


class FinFETCatalogTest(unittest.TestCase):
    def test_metal_and_dielectric_are_native_catalog_materials(self) -> None:
        rows = {row["name"]: int(row["id"]) for row in material_catalog()["materials"]}
        self.assertEqual(rows["Metal"], 5)
        self.assertEqual(rows["Dielectric"], 4)

    def test_finfet_material_references_are_tracked(self) -> None:
        names = referenced_material_names(project_payload())
        for name in ("Metal", "Dielectric", "TiN", "SiGe", "HfO2", "Si3N4", "PolySi"):
            self.assertIn(name, names)


class FinFETStepSchemaTest(unittest.TestCase):
    def test_insert_box_and_remove_top_level_set_are_discriminated(self) -> None:
        parsed, _ = validate_project(project_payload(), kind="simulate")
        models = [step.model for step in parsed.steps]
        self.assertEqual(models[0], "insertBox")
        self.assertIn("removeTopLevelSet", models)
        box = parsed.steps[0]
        self.assertIsInstance(box, InsertBoxStepV7)
        self.assertEqual(box.material, "Mask")

    def test_particle_shape_deposit_and_mask_extensions(self) -> None:
        step = ParticleShapeStepV7(
            id="step_shape_dep_01", name="dep", enabled=True, operation="deposit",
            model="particleShape", shape="sphere", radius=0.01, depositMaterial="Metal")
        self.assertEqual(step.depositMaterial, "Metal")
        with self.assertRaises(ValueError):
            ParticleShapeStepV7(
                id="step_shape_dep_02", name="bad", enabled=True, operation="deposit",
                model="particleShape", shape="sphere", radius=0.01,
                maskMaterials=["Mask"], maskMode="explicit")

    def test_masked_deposition_is_rejected_by_validation(self) -> None:
        payload = project_payload()
        payload["steps"] = [dict(payload["steps"][1], maskMaterials=["Mask"], maskMode="explicit")]
        with self.assertRaises(ProjectInputError):
            validate_project(payload, kind="simulate")

    def test_isotropic_material_mode_allows_zero_uniform_rate(self) -> None:
        payload = project_payload()
        payload["steps"] = [{
            "id": "step_fin_release", "name": "fin release", "enabled": True,
            "operation": "etch", "model": "isotropic", "rateMagnitude": 0.0,
            "duration": 5.0, "rateMode": "material", "materialRates": {"Si": 1.0},
            "advection": {"spatialScheme": "LAX_FRIEDRICHS_2ND_ORDER"},
        }]
        parsed, _ = validate_project(payload, kind="simulate")
        step = parsed.steps[0]
        self.assertEqual(step.rateMode, "material")
        self.assertEqual(step.rateMagnitude, 0.0)
        self.assertEqual(step.advection.spatialScheme, "LAX_FRIEDRICHS_2ND_ORDER")

    def test_directional_native_signs_are_explicitly_opt_in(self) -> None:
        payload = project_payload()
        step = dict(payload["steps"][4])
        self.assertEqual(step["rateSigns"], "native")
        parsed, _ = validate_project(payload, kind="simulate")
        self.assertEqual(parsed.steps[4].rateSigns, "native")
        # Without the opt-in the historical non-negative rule still applies.
        step["rateSigns"] = None
        step["materialRates"] = {"Si": {"directional": 0.001, "isotropic": -0.0001}}
        payload["steps"] = [step]
        with self.assertRaises(ProjectInputError):
            validate_project(payload, kind="simulate")

    def test_step_advection_needs_a_scheme_when_max_dissipation_is_used(self) -> None:
        from process_models.v2.engines.viennaps import configure_advection
        import viennaps as ps

        project = project_payload()
        with self.assertRaises(ValueError):
            configure_advection(ps, project, override={"maxDissipation": 100.0})
        _params, actual = configure_advection(
            ps, project,
            override={"spatialScheme": "STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER",
                      "maxDissipation": 100.0})
        self.assertEqual(actual["maxDissipation"], 100.0)
        self.assertEqual(actual["spatialScheme"], "STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER")

    def test_volume_mesh_delta_requires_volume_mesh(self) -> None:
        payload = project_payload()
        payload["outputSettings"]["volumeMesh"] = False
        with self.assertRaises(ProjectInputError):
            validate_project(payload, kind="simulate")

    def test_finfet_project_hash_is_stable(self) -> None:
        parsed, _ = validate_project(project_payload(), kind="simulate")
        first = requested_input_hash(parsed)
        again, _ = validate_project(project_payload(), kind="simulate")
        self.assertEqual(first, requested_input_hash(again))


if __name__ == "__main__":
    unittest.main()
