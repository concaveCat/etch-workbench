"""Stage 8 schema, installed-model capability, replay and evidence contracts."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from backend.hashing import requested_input_hash
from backend.rates import has_motion, uses_flux
from backend.engine import engine_version
from contextlib import nullcontext
from unittest.mock import patch


def measured_engine(version: str = "4.6.2"):
    """Pin the runtime this catalog contract was measured against.

    2D FaradayCageEtching is a measured no-op on 4.7.0, so its admission is
    version-qualified and the 4.6.2-era catalog assertion pins 4.6.2.
    """
    if engine_version() == version:
        return nullcontext()
    return patch("backend.engine.engine_version", return_value=version)
from backend.specialized_models import specialized_catalog
from backend.validation import ProjectInputError, capability_verified, gpu_project_signature, select_engine, validate_project
from stage8_project_factory import make_project


class Stage8SchemaContracts(unittest.TestCase):
    def test_each_installed_model_is_a_schema7_project_without_changing_schema_version(self):
        catalog = specialized_catalog()
        self.assertEqual(catalog["engineVersion"], engine_version())
        self.assertEqual(len(catalog["models"]), 16)
        for spec in catalog["models"]:
            for dimension in spec.get("dimensions") or (2, 3):
                with self.subTest(model=spec["id"], dimension=dimension), measured_engine():
                    project, _ = validate_project(make_project(spec["id"], dimension), kind="simulate")
                    self.assertEqual(project.schemaVersion, 7)
                    self.assertEqual(project.steps[0].model, "specialized")
                    self.assertEqual(project.steps[0].nativeModel, spec["id"])
                    self.assertTrue(has_motion(project.steps[0]))
                    self.assertEqual(uses_flux(project.steps[0]), spec["usesFlux"])

    def test_native_inputs_change_project_hash_and_reject_unknown_or_malformed_values(self):
        source = make_project("sf6o2")
        first = requested_input_hash(validate_project(source)[0])
        edited = copy.deepcopy(source)
        edited["steps"][0]["nativeParameters"]["oxygenFlux"] = 0.7
        self.assertNotEqual(first, requested_input_hash(validate_project(edited)[0]))
        edited["steps"][0]["nativeModel"] = "not-installed"
        with self.assertRaises(Exception):
            validate_project(edited)
        edited = copy.deepcopy(source)
        edited["steps"][0]["nativeParameters"]["ionFlux"] = float("nan")
        with self.assertRaises(ProjectInputError):
            validate_project(edited)

    def test_native_material_preconditions_are_enforced_before_entering_cpp(self):
        project = make_project("fluorocarbon")
        del project["steps"][0]["nativeParameters"]["materialParametersByMaterial"]["Mask"]
        with self.assertRaises(ProjectInputError):
            validate_project(project)
        project = make_project("selectiveEpitaxy")
        project["steps"][0]["nativeParameters"]["materialRates"]["SiGe"] = 0.0
        with self.assertRaises(ProjectInputError):
            validate_project(project)
        project = make_project("oxideRegrowth")
        project["geometry"]["layers"] = [project["geometry"]["layers"][0]]
        with self.assertRaises(ProjectInputError):
            validate_project(project)
        project = make_project("oxideRegrowth")
        project["geometry"]["pattern"] = {"kind": "trench", "width": 1.0, "taperAngle": 0.0}
        with self.assertRaises(ProjectInputError):
            validate_project(project)

    def test_gpu_specialized_model_routes_even_without_a_matching_stage9_row(self):
        project = make_project("sf6o2")
        project["solver"]["enginePreference"] = "gpu"
        parsed, _ = validate_project(project, kind="simulate")
        self.assertEqual(parsed.solver.enginePreference, "gpu")
        project["domain"]["xBoundary"] = "periodic"
        parsed, _ = validate_project(project, kind="simulate")
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")

    def test_cpu_rows_and_host_verified_gpu_rows_are_exact_model_dimension_and_boundary(self):
        experimental = {model["id"] for model in specialized_catalog()["models"] if model.get("experimental")}
        # These two entries have real native smoke tests but no archived Stage
        # 8 direct-reference row yet; they must never be reported as verified.
        self.assertEqual(experimental, {"faradayCage", "geometricTrenchDeposition"})
        for model in specialized_catalog()["models"]:
            for dimension in model.get("dimensions") or (2, 3):
                with self.subTest(model=model["id"], dimension=dimension):
                    engine = "CPU_DISK" if model["usesFlux"] else "not-applicable"
                    key = f"specialized:{model['id']}"
                    project = make_project(model["id"], dimension)
                    signature = gpu_project_signature(project, project["steps"][0])
                    if model["id"] in experimental:
                        self.assertFalse(capability_verified(dimension, key, model["operation"], "reflective", engine,
                                                             schema_version=7, **signature))
                        continue
                    self.assertTrue(capability_verified(dimension, key, model["operation"], "reflective", engine,
                                                        schema_version=7, **signature))
                    self.assertFalse(capability_verified(dimension, key, model["operation"], "periodic", engine, schema_version=7))
                    self.assertEqual(capability_verified(dimension, key, model["operation"], "reflective", "GPU_DISK",
                                                         schema_version=7, **signature),
                                     model["usesFlux"])
                    self.assertFalse(capability_verified(dimension, key, model["operation"], "periodic", "GPU_DISK",
                                                         schema_version=7, **signature))

    def test_independent_reference_matrix_contains_mesh_and_native_process_evidence(self):
        evidence = ROOT / "docs" / "stage8-evidence" / "product-worker"
        summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["passed"])
        self.assertEqual(len(summary["rows"]), 28)
        for row in summary["rows"]:
            with self.subTest(model=row["model"], dimension=row["dimension"]):
                self.assertEqual(row["status"], "succeeded")
                self.assertTrue(row["directReference"]["comparisons"]["initial"]["points"])
                self.assertTrue(row["directReference"]["comparisons"]["final"]["points"])
                self.assertTrue(row["actualExecution"])


if __name__ == "__main__":
    unittest.main()
