"""Compatibility and admission checks for the isolated native GPU candidate."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.hashing import execution_hash
from backend.models import ReplayRequirements
from backend.native_build import fingerprint_package
from backend.validation import ProjectInputError, select_engine, validate_project

ROOT = Path(__file__).resolve().parents[1]
EIGHT_ION_PROJECT = (ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe"
                     / "operator-split/eight-ions-2d-128rays.project.json")


class MultiIonGpuRuntimePolicyTests(unittest.TestCase):
    """Stock 4.7.0 GPU engines keep one ion flux field and drop the rest."""

    def project(self, ions: int, preference: str = "gpu") -> dict:
        project = json.loads(EIGHT_ION_PROJECT.read_text(encoding="utf-8"))
        step = project["steps"][0]
        step["species"] = step["species"][:ions]
        labels = [row["label"] for row in step["species"]]
        step["rateCoefficients"] = {"defaultBySpecies": {name: 0.025 for name in labels}}
        project["solver"]["enginePreference"] = preference
        return project

    def test_stock_47_rejects_multi_ion_gpu_with_an_actionable_policy(self):
        with patch("backend.engine.engine_version", return_value="4.7.0"):
            with self.assertRaises(ProjectInputError) as context:
                validate_project(self.project(2), kind="simulate")
            messages = " ".join(issue.message for issue in context.exception.issues)
            self.assertIn("silently drop every ion after the first", messages)
            self.assertIn("4.6.2 patched runtime", messages)
            # One ion keeps working, and CPU_DISK is the in-version route.
            validate_project(self.project(1), kind="simulate")
            cpu = validate_project(self.project(2, preference="cpu"), kind="simulate")[0]
            self.assertEqual(select_engine(cpu.model_dump(mode="json"),
                                           cpu.model_dump(mode="json")["steps"][0]), "CPU_DISK")

    def test_patched_462_runtime_keeps_accepting_multi_ion_gpu(self):
        with patch("backend.engine.engine_version", return_value="4.6.2"):
            parsed = validate_project(self.project(2), kind="simulate")[0]
        normalized = parsed.model_dump(mode="json")
        self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")


class MultiIonGpuFluxPolicyTests(unittest.TestCase):
    """The single-pass flux path must apply the same policy as the process path.

    A direct worker caller reaches ``calculate_single_flux`` without passing
    ``backend.validation`` or ``backend.supervisor``, which is how a 4.7.0
    ``GPU_DISK`` two-ion ``flux_strategy=None`` request used to reach
    ``Process.calculateFlux()`` and come back truncated.
    """

    FLUX = "per-species-native-mesh-merge-v1"
    NATIVE_FLUX = "native-multi-ion-flux-v1"

    def flux_project(self, dimension: int, ions: int = 2) -> dict:
        from tests.test_stage7 import multi_project
        project = multi_project(dimension, "etch")
        step = project["steps"][0]
        template = step["species"][0]
        step["species"] = []
        for index in range(ions):
            species = json.loads(json.dumps(template))
            species["label"] = f"ion{index}"
            step["species"].append(species)
        labels = [row["label"] for row in step["species"]]
        step["rateCoefficients"] = {"defaultBySpecies": {name: 0.025 for name in labels},
                                    "byMaterial": {"SiO2": {name: 0.04 for name in labels}}}
        project["solver"]["enginePreference"] = "gpu"
        return project

    def test_direct_gpu_flux_is_rejected_for_two_ions_in_2d_and_3d(self):
        from backend.engine import multi_ion_gpu_error
        from worker.simulate import calculate_single_flux

        policy = multi_ion_gpu_error()
        self.assertIsNotNone(policy)
        for dimension in (2, 3):
            project = self.flux_project(dimension)
            step = project["steps"][0]
            for engine in ("GPU_DISK", "GPU_TRIANGLE"):
                for strategy in (None, self.FLUX, self.NATIVE_FLUX):
                    with self.subTest(dimension=dimension, engine=engine, strategy=strategy):
                        # domain=None proves the gate fires before any native
                        # model construction, flux calculation or GPU context.
                        with self.assertRaises(RuntimeError) as context:
                            calculate_single_flux(None, None, project, step, engine, 271828,
                                                  Path("/nonexistent"), flux_strategy=strategy)
                        self.assertEqual(str(context.exception), policy)

    def test_cpu_multi_ion_and_single_ion_gpu_are_not_gated(self):
        from worker.simulate import (multi_ion_gpu_error, multi_ion_gpu_step,
                                     require_multi_ion_gpu_policy)

        self.assertIsNone(multi_ion_gpu_error("4.6.2"))
        project = self.flux_project(2)
        two_ions = project["steps"][0]
        single_ion = {**two_ions, "species": two_ions["species"][:1]}
        for engine, step, expected in (("CPU_DISK", two_ions, False),
                                       ("CPU_TRIANGLE", two_ions, False),
                                       ("GPU_DISK", single_ion, False),
                                       ("GPU_TRIANGLE", single_ion, False),
                                       ("GPU_DISK", {"model": "singleParticle"}, False),
                                       ("GPU_DISK", two_ions, True),
                                       ("GPU_TRIANGLE", two_ions, True)):
            with self.subTest(engine=engine, model=step.get("model")):
                self.assertIs(multi_ion_gpu_step(step, engine), expected)
                if not expected:
                    self.assertIsNone(require_multi_ion_gpu_policy(step, engine))

    def test_truncated_native_flux_is_rejected_even_without_a_strategy(self):
        from worker.simulate import require_multi_ion_flux_fields

        step = self.flux_project(2)["steps"][0]
        truncated = {"pointData": {"scalars": {"ion00": [1.0]}},
                     "cellData": {"scalars": {"MaterialIds": [10]}}}
        complete = {"pointData": {"scalars": {"ion00": [1.0], "ion11": [1.0]}},
                    "cellData": {"scalars": {"ion00": [1.0], "ion11": [1.0]}}}
        for strategy in (None, self.NATIVE_FLUX):
            with self.subTest(strategy=strategy):
                with self.assertRaises(RuntimeError) as context:
                    require_multi_ion_flux_fields(truncated, step, strategy)
                self.assertIn("missing species fields", str(context.exception))
                self.assertIn("truncated multi-ion flux", str(context.exception))
                self.assertIn("ion11", str(context.exception))
                require_multi_ion_flux_fields(complete, step, strategy)
        # The per-species merge route owns its own field check.
        require_multi_ion_flux_fields(truncated, step, self.FLUX)

    def test_cpu_multi_ion_flux_still_emits_every_species_field(self):
        import viennaps as ps
        from backend.engine import engine_version
        from worker.simulate import calculate_single_flux

        if engine_version() != "4.7.0":
            self.skipTest("the CPU multi-ion flux probe is pinned to the default runtime")
        project = self.flux_project(2)
        step = project["steps"][0]
        project["solver"]["enginePreference"] = "cpu"
        ps.setDimension(2)
        ps.setNumThreads(1)
        domain = ps.Domain(0.2, 2.4, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
        ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
        run_dir = Path(tempfile.mkdtemp(prefix="multi-ion-flux-", dir=str(ROOT / "var")))
        self.addCleanup(__import__("shutil").rmtree, run_dir, ignore_errors=True)
        result = calculate_single_flux(ps, domain_domain := domain, project, step, "CPU_DISK",
                                       271828, run_dir, flux_strategy=None)
        self.assertEqual(result["actualEngine"], "CPU_DISK")
        self.assertIn("ion00", result["fieldNames"])
        self.assertIn("ion11", result["fieldNames"])
        self.assertTrue(domain_domain.getSurfaceMesh().getNodes())


class NativeMultiIonPolicyTests(unittest.TestCase):
    def test_historical_split_route_profiles_are_resolved_and_not_gpu_admission_gates(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        self.assertEqual(matrix["knownGpuProfileFailures"], [])
        failures = matrix["historicalSplitRouteGpuProfileDiagnostics"]
        self.assertEqual(len(failures), 6)
        self.assertEqual(matrix["resolvedGpuMultiIonSupport"]["resolvedSolverProfileRechecks"]["count"], 6)
        self.assertTrue((ROOT / matrix["resolvedGpuMultiIonSupport"]["resolvedSolverProfileRechecks"]["browserEvidence"]).is_file())
        for failure in failures:
            project = json.loads((ROOT / failure["evidence"]).parent.joinpath("project.json").read_text())
            project["solver"].update(failure["solverProfile"])
            project["solver"]["enginePreference"] = "gpu"
            with self.subTest(profile=failure["solverProfile"], dimension=project["dimension"]):
                self.assertEqual(select_engine(project, project["steps"][0]), "GPU_DISK")
                # These profiles are 4.6.2 measurements; the multi-ion GPU
                # policy is version-qualified, so pin the recorded runtime.
                with patch("backend.engine.engine_version", return_value="4.6.2"):
                    validate_project(project, kind="simulate")

    def test_old_replay_shape_and_new_engine_identity(self):
        old = {"viennapsVersion": "4.6.2", "viennalsVersion": "5.8.5", "cpuThreads": 1,
               "steps": [{"stepId": "particle-step", "engine": "GPU_DISK", "seed": 123,
                          "executionStrategy": "symmetric-operator-splitting-v1"}]}
        self.assertEqual(ReplayRequirements.model_validate(old).model_dump(mode="json", exclude_none=True), old)
        new = {**old, "nativeEngineFingerprint": "a" * 64,
               "steps": [{**old["steps"][0], "executionStrategy": "native-multi-ion-v1"}]}
        self.assertEqual(ReplayRequirements.model_validate(new).model_dump(mode="json", exclude_none=True), new)
        args = dict(run_seed=123, cpu_threads=1, step_engines=old["steps"],
                    viennaps_version="4.6.2", viennals_version="5.8.5")
        legacy_hash = execution_hash("b" * 64, **args)
        self.assertEqual(legacy_hash, execution_hash("b" * 64, native_engine_fingerprint=None, **args))
        self.assertNotEqual(legacy_hash, execution_hash("b" * 64, native_engine_fingerprint="a" * 64, **args))

    def test_engine_fingerprint_tracks_extension_gpu_library_and_kernels(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "viennaps"
            (package / "ptx").mkdir(parents=True)
            (package / "__init__.py").write_text("# isolated package\n")
            libs = Path(tmp) / "viennaps.libs"
            libs.mkdir()
            embree = libs / "libembree4.so"
            embree.write_bytes(b"embree")
            (libs / "libtbb.so").write_bytes(b"tbb")
            (package / "_core.test.so").write_bytes(b"extension")
            (package / "libViennaLS_GPU.so").write_bytes(b"gpu-abi")
            kernel = package / "ptx" / "ViennaPSCallableWrapper.optixir"
            kernel.write_bytes(b"kernel")
            first = fingerprint_package(package)
            self.assertEqual(first, fingerprint_package(package))
            kernel.write_bytes(b"changed kernel")
            self.assertNotEqual(first, fingerprint_package(package))
            kernel.write_bytes(b"kernel")
            embree.write_bytes(b"changed embree")
            self.assertNotEqual(first, fingerprint_package(package))


if __name__ == "__main__":
    unittest.main()
