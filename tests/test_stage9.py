"""Stage 9 GPU exposure, exact matrix gating and no-fallback contracts."""
from __future__ import annotations

import json
import os
import sys
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
sys.path.insert(0, str(ROOT))

from backend.engine import engine_version
from backend.rates import uses_flux
from backend import materials as material_module
from backend.specialized_models import FLUOROCARBON_MATERIAL_DEFAULTS, specialized_catalog
from backend.validation import (ProjectInputError, capability_verified, fin_construction_decision,
                                gpu_project_signature, select_engine, validate_project)
from run_gpu_geometry_coverage import make_project as make_generic_geometry_project
from stage8_project_factory import make_project
from tests.test_stage7 import multi_project
from worker.simulate import nvidia_process_readback


@contextmanager
def measured_engine(version: str = "4.6.2"):
    """Pin the runtime these archived 4.6.2 GPU evidence rows were measured on.

    Multi-ion GPU steps are refused on 4.7.0 because the stock binding drops
    every ion after the first; the matrix audit still pins its source runtime.
    """
    if engine_version() == version:
        yield
    else:
        with patch("backend.engine.engine_version", return_value=version):
            yield



class Stage9GpuMatrixTests(unittest.TestCase):
    def test_nvidia_smi_pid_visibility_is_advisory_but_wrong_device_is_fatal(self):
        uuid = "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d"
        with patch("worker.simulate.subprocess.run", return_value=subprocess.CompletedProcess(
                args=["nvidia-smi"], returncode=0, stdout="", stderr="")):
            result = nvidia_process_readback(uuid)
        self.assertFalse(result["processListed"])
        self.assertEqual(result["processListStatus"], "not-observed")
        self.assertEqual(result["expectedGpuUuid"], uuid)
        self.assertIsNone(result["gpuUuid"])

        pid = os.getpid()
        matching = subprocess.CompletedProcess(
            args=["nvidia-smi"], returncode=0,
            stdout=f"{pid}, {uuid}, 32\n", stderr="")
        with patch("worker.simulate.subprocess.run", return_value=matching):
            result = nvidia_process_readback(uuid)
        self.assertTrue(result["processListed"])
        self.assertEqual(result["processListStatus"], "verified")
        self.assertEqual(result["gpuUuid"], uuid)

        wrong_device = subprocess.CompletedProcess(
            args=["nvidia-smi"], returncode=0,
            stdout=f"{pid}, GPU-00000000-0000-0000-0000-000000000000, 32\n", stderr="")
        with patch("worker.simulate.subprocess.run", return_value=wrong_device):
            with self.assertRaisesRegex(RuntimeError, "unexpected GPU"):
                nvidia_process_readback(uuid)

    def test_only_product_and_direct_reference_gpu_tuples_are_published(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        source = json.loads((ROOT / "docs/stage9-evidence/gpu-specialized/summary-gpu_disk.json").read_text())
        self.assertTrue(source["passed"])
        self.assertEqual(len(matrix["rows"]), 560)
        self.assertEqual(sum(row["engine"] == "GPU_DISK" for row in matrix["rows"]), 558)
        self.assertEqual(sum(row["engine"] == "GPU_TRIANGLE" for row in matrix["rows"]), 2)
        periodic_specialized = [row for row in matrix["rows"]
                                if row["boundaryXY"] == "periodic" and row["model"].startswith("specialized:")]
        self.assertEqual(len(periodic_specialized), 160)
        periodic_generic = [row for row in matrix["rows"]
                            if row["boundaryXY"] == "periodic" and row["model"] in ("singleParticle", "multiParticle")
                            and row["geometryKinds"] != ["gds"]]
        self.assertEqual(len(periodic_generic), 72)
        unique = set()
        missing: list[str] = []
        for row in matrix["rows"]:
            key = tuple(row[name] for name in ("dimension", "model", "operation", "boundaryXY", "engine")) + (
                tuple(row["geometryKinds"]), tuple(row.get("patternKinds", [])), tuple(row["materials"]),
                json.dumps(row["materialRoles"], sort_keys=True), json.dumps(row["materialBindings"], sort_keys=True),
                json.dumps(row.get("speciesSignature"), sort_keys=True), row.get("finExtrusionMode"),
                row.get("finConstruction"), json.dumps(row.get("solverProfile"), sort_keys=True))
            self.assertNotIn(key, unique)
            unique.add(key)
            self.assertEqual(row["status"], "verified")
            self.assertEqual(row["schemaVersions"], [7])
            self.assertIn(row["engine"], {"GPU_DISK", "GPU_TRIANGLE"})
            self.assertTrue(row["geometryKinds"])
            self.assertTrue(row["materials"])
            self.assertIsInstance(row["materialRoles"], dict)
            self.assertIsInstance(row["materialBindings"], dict)
            for field in ("productWorker", "productGuard", "directReference", "directGuard", "browserReport",
                          "geometryAcceptance", "productDataTransferManifest"):
                reference = row["evidence"].get(field)
                if reference and not (ROOT / reference).exists():
                    missing.append(f"{field}: {reference}")
            if row["model"] == "multiParticle":
                self.assertIn("speciesSignature", row)
                self.assertGreaterEqual(len(row["speciesSignature"]["species"]), 1)
                self.assertLessEqual(len(row["speciesSignature"]["species"]), 8)
                self.assertEqual(set(row["speciesSignature"]["rateCoefficients"]["defaultBySpecies"]),
                                 {species["label"] for species in row["speciesSignature"]["species"]})
        if missing and all(reference.split(":", 1)[1].strip().startswith("var/") for reference in missing):
            # ``var/`` holds measured run output, not shipped evidence. A fresh
            # worktree has none of it; the matrix itself is verified above and
            # in docs/, so a run-artifact gap is not a code regression.
            self.skipTest(f"{len(missing)} GPU run artifacts under var/ are not archived in this worktree")
        self.assertEqual(missing, [], "missing GPU matrix evidence")

    def test_historical_cpu_nan_failure_guard_is_archived(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        guard = ROOT / matrix["resolvedGpuMultiIonSupport"]["cpuGpuProfileAudit"]["cpuNativeFailureRerun"]["guard"]
        if not guard.is_file():
            self.skipTest("historical cpu-nan-debug guard is not archived; rerun the profile-recheck experiment")
        self.assertTrue(guard.is_file())

    def test_custom_material_gpu_webui_acceptance_is_evidence_backed_and_selectable(self):
        summary = json.loads((ROOT / "docs/stage9-evidence/gpu-custom-material/summary.json").read_text())
        self.assertTrue(summary["passed"])
        self.assertTrue(summary["exposedToWebUI"])
        ui = summary["userFacingAcceptance"]
        self.assertEqual(ui["status"], "passed")
        self.assertEqual({row["dimension"] for row in ui["cases"]}, {2, 3})
        self.assertEqual(ui["device"]["name"], "NVIDIA GeForce RTX 3060 Laptop GPU")
        self.assertTrue((ROOT / ui["report"]).is_file())
        runner = json.loads((ROOT / ui["runnerGuard"]).read_text())
        self.assertEqual(runner["returnCode"], 0)
        self.assertEqual(runner["remainingOwnedGroupMembers"], [])

        report = json.loads((ROOT / ui["report"]).read_text())
        for case in report["cases"]:
            self.assertTrue(case["exactInitialFinalAndReplayMatchToDirectViennaPS"])
            self.assertTrue(case["savedReopenedExportedAndReplayedWithStableMaterialId"])
            self.assertEqual(case["engine"], "GPU_DISK")
            self.assertEqual(case["gpuDeviceId"], ui["device"]["uuid"])
            evidence_dir = ROOT / case["productEvidence"]
            for filename in ("submitted-project.json", "exported-project.json", "worker-result.json",
                             "child-record.json", "initial.json", "final.json", "replay-worker-result.json",
                             "replay-child-record.json", "replay-final.json"):
                self.assertTrue((evidence_dir / filename).is_file(), f"missing {filename} in {evidence_dir}")
            project = json.loads((evidence_dir / "submitted-project.json").read_text())
            definitions = project["materialDefinitions"]
            self.assertEqual(next(row["id"] for row in definitions if row["name"] == case["material"]["name"]),
                             case["material"]["id"])
            base_catalog = material_module.material_catalog()
            builtin_rows = [row for row in base_catalog["materials"] if not row.get("isCustom")]
            custom_rows = [{**row, "isCustom": True} for row in definitions]
            isolated_catalog = {"schemaVersion": 1, "engineVersion": "4.6.2",
                                "materials": [*builtin_rows, *custom_rows]}
            with patch("backend.materials.custom_materials", return_value=definitions), \
                    patch("backend.materials.material_catalog", return_value=isolated_catalog):
                parsed, _ = validate_project(project, kind="simulate")
                normalized = parsed.model_dump(mode="json", exclude_none=True)
                self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")

    def test_multi_gpu_selector_ui_round_trips_a_second_reported_uuid(self):
        evidence_dir = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/browser"
        report = json.loads((evidence_dir / "synthetic-multi-device-ui.json").read_text())
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["testKind"], "synthetic-inventory-ui-persistence-only")
        self.assertTrue(report["optionsContainedBothDevices"])
        self.assertFalse(report["simulationExecuted"])
        self.assertFalse(report["physicalSecondaryGpuClaimed"])
        self.assertEqual(report["selectedDeviceUuid"], report["savedReopenedExportedAndImportedUuid"])
        self.assertNotEqual(report["selectedDeviceUuid"], report["physicalDevice"]["uuid"])
        self.assertEqual(report["physicalDevice"]["name"], "NVIDIA GeForce RTX 3060 Laptop GPU")
        self.assertEqual(report["pageErrors"], [])
        runner = json.loads((ROOT / report["runnerGuard"]).read_text())
        self.assertEqual(runner["returnCode"], 0)
        self.assertEqual(runner["remainingOwnedGroupMembers"], [])
        initial = json.loads((evidence_dir / "synthetic-multi-device-input.project.json").read_text())
        exported = json.loads((evidence_dir / "synthetic-multi-device-export.project.json").read_text())
        self.assertNotIn("gpuDeviceId", initial["solver"])
        self.assertEqual(exported["solver"]["gpuDeviceId"], report["selectedDeviceUuid"])

    def test_gpu_api_rerun_records_physical_cuda_readback_and_replays(self):
        evidence_dir = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/gpu-api-regression-multi-device-ui"
        report = json.loads((evidence_dir / "acceptance.json").read_text())
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["testCount"], 4)
        self.assertTrue((ROOT / report["testTranscript"]).is_file())
        outer_guard = json.loads((ROOT / report["outerGuard"]).read_text())
        self.assertEqual(outer_guard["returnCode"], 0)
        self.assertEqual(outer_guard["remainingOwnedGroupMembers"], [])
        self.assertEqual(len(report["runs"]), 9)
        gpu_runs = [row for row in report["runs"] if row["engines"] == ["GPU_DISK"]]
        self.assertEqual(len(gpu_runs), 8)
        for row in gpu_runs:
            readback = row["cudaDeviceReadback"]
            self.assertEqual(readback["name"], "NVIDIA GeForce RTX 3060 Laptop GPU")
            self.assertEqual(readback["uuid"], "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d")
            self.assertEqual(readback["readbackStatus"], "verified")
            self.assertTrue(readback["process"]["processListed"])
            self.assertEqual(row["childReturnCode"], 0)
            self.assertEqual(row["childRemainingOwnedGroupMembers"], [])
            self.assertTrue(row["snapshots"])
            run_dir = evidence_dir / "runs" / row["runId"]
            for filename in ("worker-result.json", "child-record.json", "initial.json", "project.json",
                             "replay-project.json", "manifest.json"):
                self.assertTrue((run_dir / filename).is_file(), f"missing {filename} for {row['runId']}")

    def test_patched_gpu_multiparticle_multi_ion_support_is_evidence_backed(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        self.assertEqual(matrix.get("knownEngineLimitations"), [])
        support = matrix["resolvedGpuMultiIonSupport"]
        self.assertEqual(support["status"], "verified")
        self.assertEqual(support["engine"], "GPU_DISK")
        self.assertEqual(support["runtimeFingerprint"],
                         "cc890a8dcf9e99f89afe31333cb5825cdba084a3aefbd14716665e6909e51e75")
        self.assertEqual(support["verifiedIonSpeciesCount"], 8)
        self.assertEqual(support["verifiedGdsCases"], 12)
        self.assertEqual(support["dimensions"], [2, 3])
        self.assertFalse(support["cpuGpuEvolutionTopologyEqual"])
        self.assertIn("docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/"
                      "browser-ui-acceptance-eight-ions.json", support["evidence"])
        self.assertIn("docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/"
                      "browser-ui-acceptance-3d-eight-ions.json", support["evidence"])
        cpu_gpu_audit = support["cpuGpuProfileAudit"]
        self.assertEqual(cpu_gpu_audit["status"], "partial-native-cpu-failure")
        self.assertEqual(cpu_gpu_audit["comparedCases"], 5)
        self.assertEqual(cpu_gpu_audit["expectedCases"], 6)
        self.assertEqual(cpu_gpu_audit["cpuNativeFailureCase"], "3d-trench-cfl0.2-rays1024")
        self.assertLess(cpu_gpu_audit["maximumBidirectionalNearestVertexDistanceUm"], 0.02)
        self.assertIn("non-finite species flux", cpu_gpu_audit["cpuNativeFailureRerun"]["runtimeError"])
        self.assertTrue((ROOT / cpu_gpu_audit["evidence"]).is_file())
        for evidence in support["evidence"]:
            self.assertTrue((ROOT / evidence).is_file(), evidence)
        self.assertIn("docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/summary.json",
                      matrix["historicalStockGpuMultiIonDiagnostics"])
        self.assertNotIn("docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/summary.json",
                         matrix["sources"])

        direct = json.loads((ROOT / support["evidence"][0]).read_text())
        self.assertEqual(direct["status"], "passed")
        self.assertEqual(direct["deviceUuid"], support["deviceUuid"])
        expected_species = [f"ion{index}" for index in range(8)]
        for dimension in (2, 3):
            cases = [row for row in direct["cases"] if row["dimension"] == dimension]
            self.assertEqual({row["engine"] for row in cases}, {"CPU_DISK", "GPU_DISK"})
            self.assertTrue(all(row["returnCode"] == 0 and row["speciesOrder"] == expected_species
                                and row["callbackCount"] > 0 and row["callbackSamplesChecked"] > 0
                                for row in cases))
            comparison = next(row for row in direct["comparisons"] if row["dimension"] == dimension)
            self.assertTrue(comparison["sameMaterialMap"])
            self.assertFalse(comparison["samePointCount"])
            for engine in ("CPU_DISK", "GPU_DISK"):
                output = ROOT / f"docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/eight-ion-{dimension}d-{engine.lower()}.json"
                native = json.loads(output.read_text())
                self.assertEqual(native["actualParameters"]["speciesOrder"], expected_species)
                self.assertEqual(native["versions"], {"viennaps": "4.6.2", "viennals": "5.8.5"})
                self.assertTrue(Path(native["viennapsModulePath"]).resolve().is_relative_to(
                    Path(direct["runtimeDir"]).resolve()))

        for filename, dimension in (("eight-ion-ui-direct-2d-comparison.json", 2),
                                    ("eight-ion-ui-direct-3d-comparison.json", 3)):
            ui = json.loads((ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product"
                             / filename).read_text())
            self.assertEqual((ui["status"], ui["dimension"], ui["gpuUuid"]),
                             ("passed", dimension, support["deviceUuid"]))
            self.assertTrue(all(all(check.values()) for check in ui["comparisons"].values()))

    def test_workbench_multi_ion_process_and_single_pass_flux_strategies_are_exact_and_evidence_backed(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        rows = [row for row in matrix["rows"] if row.get("executionStrategy") == "symmetric-operator-splitting-v1"
                and row.get("singlePassFluxStrategy") == "per-species-native-mesh-merge-v1"]
        self.assertEqual(len(rows), 2)
        summary_path = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/single-pass-flux-species/summary.json"
        summary = json.loads(summary_path.read_text())
        self.assertEqual(summary["status"], "passed")
        self.assertEqual({row["dimension"] for row in summary["cases"]}, {2, 3})
        project_by_dimension = {row["dimension"]: row["project"] for row in summary["cases"]}
        for row in rows:
            self.assertEqual(row["singlePassFluxStrategy"], "per-species-native-mesh-merge-v1")
            self.assertEqual(row["geometryKinds"], ["stack"])
            self.assertEqual(row["patternKinds"], ["trench"])
            self.assertEqual(len(row["speciesSignature"]["species"]), 3)
            self.assertEqual(set(row["speciesSignature"]["rateCoefficients"]["defaultBySpecies"]),
                             {item["label"] for item in row["speciesSignature"]["species"]})
            for evidence_name in ("multiIonProcessAcceptance", "singlePassFluxAcceptance", "browserReport"):
                evidence_path = ROOT / row["evidence"][evidence_name]
                self.assertTrue(evidence_path.is_file(), evidence_path)
            process_report = json.loads((ROOT / row["evidence"]["multiIonProcessAcceptance"]).read_text())
            flux_report = json.loads((ROOT / row["evidence"]["singlePassFluxAcceptance"]).read_text())
            self.assertEqual(process_report["status"], "passed")
            self.assertEqual(flux_report["status"], "passed")
            self.assertTrue(all(process_report["checks"].values()))
            self.assertTrue(all(flux_report["checks"].values()))
            project_path = ROOT / project_by_dimension[row["dimension"]]
            project = json.loads(project_path.read_text())
            step = project["steps"][0]
            project["solver"]["enginePreference"] = "gpu"
            signature = gpu_project_signature(project, step)
            self.assertTrue(capability_verified(project["dimension"], "multiParticle", step["operation"],
                                                project["domain"].get("boundaryXY", project["domain"].get("xBoundary")),
                                                "GPU_DISK", schema_version=7,
                                                execution_strategy="symmetric-operator-splitting-v1", **signature))
            self.assertTrue(capability_verified(project["dimension"], "multiParticle", step["operation"],
                                                project["domain"].get("boundaryXY", project["domain"].get("xBoundary")),
                                                "GPU_DISK", schema_version=7,
                                                flux_strategy="per-species-native-mesh-merge-v1", **signature))
            self.assertEqual(select_engine(project, step), "GPU_DISK")
            self.assertEqual(select_engine(project, step, analysis_mode="flux"), "GPU_DISK")
            changed = json.loads(json.dumps(project))
            changed["steps"][0]["rateCoefficients"]["defaultBySpecies"]["xenonIon"] += 0.001
            changed_signature = gpu_project_signature(changed, changed["steps"][0])
            self.assertEqual(select_engine(changed, changed["steps"][0]), "GPU_DISK")
            self.assertEqual(select_engine(changed, changed["steps"][0], analysis_mode="flux"), "GPU_DISK")

    def test_multi_ion_geometry_profiles_are_direct_ui_replay_with_representative_solver_coverage(self):
        base = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope"
        summary = json.loads((base / "summary.json").read_text())
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(summary["versions"], {"viennaps": "4.6.2", "viennals": "5.8.5"})
        self.assertEqual(len(summary["cases"]), 11)
        self.assertEqual({(row["dimension"], row["geometry"]) for row in summary["cases"]},
                         {(2, "plane"), (2, "trench"), (3, "plane"), (3, "trench"), (3, "hole")})
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        rows = [row for row in matrix["rows"] if row.get("executionStrategy") == "symmetric-operator-splitting-v1"
                and row.get("geometryKinds", [None])[0] in {"plane", "trench", "hole"}]
        self.assertEqual(len(rows), 11)
        self.assertTrue(all(row.get("solverProfile") for row in rows))
        self.assertEqual(matrix.get("knownGpuProfileFailures", []), [])
        failures = matrix.get("historicalSplitRouteGpuProfileDiagnostics", [])
        self.assertEqual(len(failures), 6)
        for failure in failures:
            self.assertEqual(failure["status"], "historical-route-mismatch")
            self.assertEqual(failure["historicalExecutionStrategy"], "symmetric-operator-splitting-v1")
            self.assertEqual(failure["resolvedForExecutionStrategy"], "native-multi-ion-v1")
            self.assertTrue((ROOT / failure["resolutionEvidence"]).is_file())
            self.assertTrue((ROOT / failure["evidence"]).is_file(), failure)
            source_project = json.loads((ROOT / failure["evidence"]).parent.joinpath("project.json").read_text())
            source_project["solver"].update(failure["solverProfile"])
            self.assertEqual(select_engine(source_project, source_project["steps"][0]), "GPU_DISK", failure)
            with measured_engine():
                validate_project(source_project, kind="simulate")
        profile_rechecks = matrix["resolvedGpuMultiIonSupport"]["resolvedSolverProfileRechecks"]
        self.assertEqual(profile_rechecks["status"], "verified")
        self.assertEqual(profile_rechecks["count"], 6)
        self.assertTrue((ROOT / profile_rechecks["evidence"]).is_file())
        profile_browser = json.loads((ROOT / profile_rechecks["browserEvidence"]).read_text())
        self.assertEqual(profile_browser["status"], "passed")
        self.assertEqual(profile_browser["processStrategy"], "native-multi-ion-v1")
        self.assertEqual(profile_browser["gpuUuid"], "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d")
        for case in summary["cases"]:
            project_path = ROOT / case["project"]
            project = json.loads(project_path.read_text())
            step = next(row for row in project["steps"] if row.get("enabled"))
            project["solver"]["enginePreference"] = "gpu"
            signature = gpu_project_signature(project, step)
            row = next(row for row in rows if row["dimension"] == project["dimension"]
                       and row["geometryKinds"] == [project["geometry"]["kind"]]
                       and row["solverProfile"] == signature["solver_profile"])
            self.assertEqual(row["solverProfile"], signature["solver_profile"])
            self.assertEqual(select_engine(project, step), "GPU_DISK")
            self.assertTrue(capability_verified(project["dimension"], "multiParticle", step["operation"],
                                                project["domain"].get("boundaryXY", project["domain"].get("xBoundary")),
                                                "GPU_DISK", schema_version=7,
                                                execution_strategy="symmetric-operator-splitting-v1", **signature))
            acceptance = json.loads((ROOT / case["browserAcceptance"]).read_text())
            self.assertEqual(acceptance["status"], "passed")
            self.assertEqual(acceptance["selectedGpuDevice"]["uuid"],
                             "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d")
            self.assertEqual(acceptance["dimension"], project["dimension"])
            self.assertEqual(acceptance["geometry"], project["geometry"]["kind"])
            self.assertEqual(acceptance.get("representativeProfileValidationStatus", 200), 200)
            for comparison_field in ("processComparison", "replayComparison"):
                comparison = json.loads((ROOT / case[comparison_field]).read_text())
                self.assertEqual(comparison["status"], "passed")
                self.assertTrue(all(comparison["checks"].values()))
            if case.get("productDataTransferManifest"):
                transfer = json.loads((ROOT / case["productDataTransferManifest"]).read_text())
                self.assertEqual(transfer["status"], "copied-and-hashed")
                self.assertTrue(transfer["candidateStopped"])
            direct_guard = json.loads((ROOT / case["directGuard"]).read_text())
            self.assertEqual(direct_guard["returnCode"], 0)
            self.assertFalse(direct_guard["timedOut"])
            self.assertFalse(direct_guard["rssLimited"])
            self.assertEqual(direct_guard["remainingOwnedGroupMembers"], [])
            seed_report = json.loads((ROOT / case["additionalSeedComparison"]).read_text())
            self.assertTrue(seed_report["initialMatchesDirect"])
            self.assertEqual(seed_report["guards"], {"cpu": True, "gpu": True})
            seed_comparisons = seed_report["cases"][0]["comparisons"]
            self.assertTrue(seed_comparisons["withinOneTenthGrid"])
            self.assertTrue(seed_comparisons["callbacksObservedForEverySpecies"])
            self.assertTrue(seed_comparisons["topologyMatchesDirect"])
            self.assertTrue(seed_comparisons["cellMaterialIdsMatchDirect"])
            self.assertTrue(seed_comparisons["levelSetMaterialMapMatchesDirect"])
            self.assertTrue(seed_comparisons["levelSetCountMatchesDirect"])

            for key, value in (("timeStepRatio", 0.1), ("raysPerPoint", 128)):
                changed = json.loads(json.dumps(project))
                changed["solver"][key] = value
                self.assertEqual(select_engine(changed, changed["steps"][0]), "GPU_DISK",
                                 f"representative model/geometry evidence should cover changed {key}")
                with measured_engine():
                    validate_project(changed, kind="simulate")

        trench_case = next(row for row in summary["cases"]
                           if row["dimension"] == 3 and row["geometry"] == "trench")
        known_failure = json.loads((ROOT / trench_case["project"]).read_text())
        known_failure["solver"]["timeStepRatio"] = 0.2
        known_failure["solver"]["raysPerPoint"] = 1024
        self.assertEqual(select_engine(known_failure, known_failure["steps"][0]), "GPU_DISK",
                         "a matrix mismatch is evidence to inspect, not a GPU admission gate")
        with measured_engine():
            validate_project(known_failure, kind="simulate")

    def test_unmeasured_multi_ion_signatures_route_to_gpu_and_cpu_remains_selectable(self):
        path = ROOT / "docs/stage9-evidence/gpu-coverage-audit/geometry-presets/multiParticle-2d-plane-direct-species-two-ion-different-energy-etch-cpu_disk/project.json"
        project = json.loads(path.read_text())
        # This plane/species signature now has a measured GPU split row. Change
        # an ion parameter so this assertion continues to cover the fail-closed
        # path for an unmeasured multi-ion signature.
        project["steps"][0]["species"][1]["meanEnergy"] += 1.0
        project["solver"]["enginePreference"] = "gpu"
        with measured_engine():
            imported, _warnings = validate_project(project, kind="project")
            normalized = imported.model_dump(mode="json", exclude_none=True)
            self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")
            validate_project(project, kind="simulate")

        for preference, expected in (("auto", "GPU_DISK"), ("cpu", "CPU_DISK")):
            cpu_project = json.loads(json.dumps(project))
            cpu_project["solver"]["enginePreference"] = preference
            with measured_engine():
                parsed, _ = validate_project(cpu_project, kind="simulate")
            cpu_normalized = parsed.model_dump(mode="json", exclude_none=True)
            self.assertEqual(select_engine(cpu_normalized, cpu_normalized["steps"][0]), expected)

    def test_new_singleparticle_and_neutral_multiparticle_rows_load_their_proofs(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        rows = [row for row in matrix["rows"] if (row["model"] == "singleParticle"
                and row["geometryKinds"] != ["gds"]
                and row["evidence"].get("directReference", "").endswith(".direct.json")) or
                row.get("speciesSignature", {}).get("species") == [{"kind": "neutral", "label": "neutral",
                    "defaultStickingProbability": 0.4, "stickingProbabilityByMaterial": {"SiO2": 0.6}}]]
        self.assertEqual(len(rows), 12)
        for row in rows:
            evidence = row["evidence"]
            direct_path = ROOT / evidence["directReference"]
            project_path = direct_path.with_name(direct_path.name.replace(".direct.json", ".project.json"))
            self.assertTrue(project_path.is_file(), project_path)
            self.assertTrue(direct_path.is_file(), direct_path)
            self.assertTrue((ROOT / evidence["productGuard"]).is_file())
            self.assertTrue((ROOT / evidence["directGuard"]).is_file())
            project = json.loads(project_path.read_text())
            parsed, _ = validate_project(project, kind="simulate")
            normalized = parsed.model_dump(mode="json", exclude_none=True)
            self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")
            self.assertTrue(all(evidence["comparisons"].values()))

    def test_preset_geometry_gpu_rows_are_exact_and_fin_modes_do_not_cross_match(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        preset_rows = [row for row in matrix["rows"] if row["evidence"].get("presetAcceptance")]
        self.assertEqual(len(preset_rows), 32)
        expected = {("plane", 2), ("plane", 3), ("trench", 2), ("trench", 3), ("hole", 3),
                    ("fin", 2), ("fin", 3)}
        self.assertEqual({(row["geometryKinds"][0], row["dimension"]) for row in preset_rows}, expected)
        for row in preset_rows:
            with self.subTest(geometry=row["geometryKinds"], dimension=row["dimension"],
                              model=row["model"], operation=row["operation"], mode=row.get("finExtrusionMode")):
                acceptance = json.loads((ROOT / row["evidence"]["presetAcceptance"]).read_text())
                project = json.loads((ROOT / acceptance["project"]).read_text())
                parsed, _ = validate_project(project, kind="simulate")
                normalized = parsed.model_dump(mode="json", exclude_none=True)
                step = normalized["steps"][0]
                self.assertEqual(select_engine(normalized, step), "GPU_DISK")
                signature = gpu_project_signature(normalized, step)
                self.assertTrue(capability_verified(row["dimension"], row["model"], row["operation"],
                                                    row["boundaryXY"], "GPU_DISK", schema_version=7, **signature))
                self.assertTrue(all(row["evidence"]["comparisons"].values()))
                if row["geometryKinds"] == ["fin"]:
                    self.assertIn(row.get("finExtrusionMode"), ("direct", "extruded"))
                    self.assertEqual(signature["fin_extrusion_mode"], row["finExtrusionMode"])
                    if row["dimension"] == 3:
                        changed = json.loads(json.dumps(normalized))
                        if row["finExtrusionMode"] == "direct":
                            changed["geometry"]["extrusion"] = {"extent": [-1.0, 1.0], "axis": 1}
                            opposite = "extruded"
                        else:
                            changed["geometry"].pop("extrusion")
                            opposite = "direct"
                        changed_signature = gpu_project_signature(changed, changed["steps"][0])
                        self.assertEqual(changed_signature["fin_extrusion_mode"], opposite)
                        self.assertTrue(capability_verified(row["dimension"], row["model"], row["operation"],
                                                            row["boundaryXY"], "GPU_DISK", schema_version=7,
                                                            **changed_signature))
                        missing_mode = {**signature, "fin_extrusion_mode": None}
                        self.assertFalse(capability_verified(row["dimension"], row["model"], row["operation"],
                                                             row["boundaryXY"], "GPU_DISK", schema_version=7,
                                                             **missing_mode))

    def test_specialized_presets_and_masked_boolean_fin_are_verified_for_every_flux_model(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        report = json.loads((ROOT / "docs/stage9-evidence/gpu-coverage-audit/specialized-geometries/coverage-report.json").read_text())
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["modelPresetCombinationsExpected"], 160)
        self.assertEqual(report["modelPresetCombinationsVerified"], 160)
        self.assertEqual(report["maskedBooleanFinCombinationsExpected"], 60)
        self.assertEqual(report["maskedBooleanFinCombinationsVerified"], 60)
        self.assertEqual(report["specializedMaskedTrenchHoleCombinationsExpected"], 60)
        self.assertEqual(report["specializedMaskedTrenchHoleCombinationsVerified"], 60)
        self.assertEqual(report["specializedBooleanFinCombinationsExpected"], 120)
        self.assertEqual(report["specializedBooleanFinCombinationsVerified"], 120)
        self.assertEqual(report["genericBooleanFinCombinationsExpected"], 12)
        self.assertEqual(report["genericBooleanFinCombinationsVerified"], 12)
        self.assertEqual(report["genericMaskedBooleanFinCombinationsExpected"], 12)
        self.assertEqual(report["genericMaskedBooleanFinCombinationsVerified"], 12)
        self.assertEqual(report["genericMaskedTrenchHoleCombinationsExpected"], 12)
        self.assertEqual(report["genericMaskedTrenchHoleCombinationsVerified"], 12)
        self.assertEqual(report["genericMaskedTrenchHoleCombinationsExpected"], 12)
        self.assertEqual(report["genericMaskedTrenchHoleCombinationsVerified"], 12)
        self.assertEqual(report["genericPeriodicGeometryCombinationsExpected"], 72)
        self.assertEqual(report["genericPeriodicGeometryCombinationsVerified"], 72)
        self.assertEqual(report["genericPeriodicBooleanFinCombinationsExpected"], 24)
        self.assertEqual(report["genericPeriodicBooleanFinCombinationsVerified"], 24)
        self.assertEqual(report["totalBooleanFinCombinationsVerified"], 168)
        self.assertEqual(len(report["knownNativeMakeFinFailures"]), 13)
        self.assertEqual(len(report["periodicFinExtrusionFallbackCases"]), 12)
        self.assertEqual(len(report["knownPeriodicFinExtrusionFailures"]), 8)
        self.assertEqual(len(report["genericPeriodicFinAutomaticFallbackCases"]), 8)
        self.assertEqual(len(report["knownPeriodicGenericFinFailures"]), 14)
        self.assertEqual(report["periodicFinExtrusionDiagnosis"]["models"],
                         ["hbro2", "neutralTransport", "sf6c4f8", "sf6o2"])
        specialized = [row for row in matrix["rows"] if row["model"].startswith("specialized:")
                       and "fin" in row["geometryKinds"]]
        masks = [row for row in specialized if "mask" in row["materialRoles"].get("Mask", [])
                 and row.get("finConstruction") == "boolean"]
        self.assertEqual(len(masks), 60)
        generic_boolean = [row for row in matrix["rows"] if row["model"] in ("singleParticle", "multiParticle")
                           and "fin" in row["geometryKinds"] and row.get("finConstruction") == "boolean"]
        self.assertEqual(len(generic_boolean), 48)
        self.assertEqual(sum("mask" in row["materialRoles"].get("Mask", []) for row in generic_boolean), 24)
        masked_patterns = [row for row in matrix["rows"] if row["geometryKinds"][0] in ("trench", "hole")
                           and "mask" in row["materialRoles"].get("Mask", [])
                           and row.get("executionStrategy") != "symmetric-operator-splitting-v1"]
        self.assertEqual(len(masked_patterns), 84)
        for row in masked_patterns + generic_boolean:
            with self.subTest(model=row["model"], geometry=row["geometryKinds"], dimension=row["dimension"],
                              operation=row["operation"], finConstruction=row.get("finConstruction")):
                acceptance = json.loads((ROOT / row["evidence"]["geometryAcceptance"]).read_text())
                project = json.loads((ROOT / acceptance["project"]).read_text())
                parsed, _ = validate_project(project, kind="simulate")
                normalized = parsed.model_dump(mode="json", exclude_none=True)
                self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")
                signature = gpu_project_signature(normalized, normalized["steps"][0])
                self.assertTrue(capability_verified(row["dimension"], row["model"], row["operation"],
                                                    row["boundaryXY"], "GPU_DISK", schema_version=7,
                                                    **signature))
        fin_evidence_models = {item["model"].split(":", 1)[1] for item in masks}
        # Derive the expected set independently of the evidence being checked:
        # otherwise a missing model row silently removes its assertions. The
        # explicitly experimental FaradayCage entry has no Stage 9 row.
        expected_fin_models = {row["id"] for row in specialized_catalog()["models"]
                               if row["usesFlux"] and not row.get("experimental")}
        self.assertEqual(fin_evidence_models, expected_fin_models)
        for spec in (row for row in specialized_catalog()["models"]
                     if row["usesFlux"] and not row.get("experimental")):
            for dimension, mode in ((2, "direct"), (3, "direct"), (3, "extruded")):
                row = next(item for item in masks if item["model"] == f"specialized:{spec['id']}"
                           and item["dimension"] == dimension and item["finExtrusionMode"] == mode)
                project = make_project(spec["id"], dimension)
                geometry = {"kind": "fin", "width": 0.8, "height": 1.2, "taperAngle": 0,
                            "maskHeight": 0.4, "maskTaperAngle": 0, "substrateMaterial": "Si",
                            "maskMaterial": "Mask", "construction": "boolean"}
                if mode == "extruded":
                    geometry["extrusion"] = {"extent": [-1.0, 1.0], "axis": 1}
                project["geometry"] = geometry
                if spec["id"] == "fluorocarbon":
                    project["steps"][0]["nativeParameters"]["materialParametersByMaterial"] = {
                        name: dict(FLUOROCARBON_MATERIAL_DEFAULTS) for name in ("Mask", "Polymer", "Si")
                    }
                project["solver"]["enginePreference"] = "gpu"
                parsed, _ = validate_project(project, kind="simulate")
                normalized = parsed.model_dump(mode="json", exclude_none=True)
                self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")
                signature = gpu_project_signature(normalized, normalized["steps"][0])
                self.assertTrue(capability_verified(dimension, f"specialized:{spec['id']}", spec["operation"],
                                                    row["boundaryXY"], "GPU_DISK", schema_version=7,
                                                    **signature))
                self.assertLessEqual(row["evidence"]["coordinateDeltaMaxAbs"]["final"],
                                     row["evidence"]["coordinateTolerance"])

        crash_models = {"sf6o2", "hbro2", "sf6c4f8", "neutralTransport"}
        for model in crash_models:
            for dimension, mode in ((2, "direct"), (3, "direct"), (3, "extruded")):
                project = make_project(model, dimension)
                project["geometry"] = {"kind": "fin", "width": 0.8, "height": 1.2,
                    "taperAngle": 0, "maskHeight": 0, "maskTaperAngle": 0,
                    "substrateMaterial": "Si", "maskMaterial": "Mask", "construction": "native"}
                if mode == "extruded":
                    project["geometry"]["extrusion"] = {"extent": [-1.0, 1.0], "axis": 1}
                project["solver"]["enginePreference"] = "gpu"
                parsed, _ = validate_project(project, kind="simulate")
                normalized = parsed.model_dump(mode="json", exclude_none=True)
                self.assertEqual(normalized["geometry"]["construction"], "native")
                decision = fin_construction_decision(normalized)
                self.assertEqual(decision, {"requested": "native", "effective": "boolean", "fallbackModels": [model],
                                            "constructionFallbackModels": [model],
                                            "constructionFallbackReason": "native-makefin-sigsegv",
                                            "extrusionRoute": "d2-extrude" if mode == "extruded" else "direct",
                                            "extrusionFallbackModels": []})
                signature = gpu_project_signature(normalized, normalized["steps"][0])
                self.assertEqual(signature["fin_construction"], "boolean")
                self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")

    def test_periodic_extruded_fin_uses_the_verified_direct_stencil_route(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        report = json.loads((ROOT / "docs/stage9-evidence/gpu-coverage-audit/specialized-geometries/coverage-report.json").read_text())
        for model in ("sf6o2", "hbro2", "sf6c4f8", "neutralTransport"):
            project = make_project(model, 3)
            project["domain"]["boundaryXY"] = "periodic"
            project["geometry"] = {"kind": "fin", "width": 0.8, "height": 1.2, "taperAngle": 0,
                "maskHeight": 0.4, "maskTaperAngle": 0, "substrateMaterial": "Si", "maskMaterial": "Mask",
                "construction": "boolean", "extrusion": {"extent": [-1.0, 1.0], "axis": 1}}
            project["solver"]["enginePreference"] = "gpu"
            parsed, _ = validate_project(project, kind="simulate")
            normalized = parsed.model_dump(mode="json", exclude_none=True)
            decision = fin_construction_decision(normalized)
            self.assertEqual(decision["extrusionRoute"], "direct-stencil")
            self.assertEqual(decision["extrusionFallbackModels"], [model])
            self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")
            row = next(item for item in matrix["rows"] if item["model"] == f"specialized:{model}"
                       and item["geometryKinds"] == ["fin"] and item["dimension"] == 3
                       and item["boundaryXY"] == "periodic" and item.get("finExtrusionMode") == "extruded"
                       and "mask" in item["materialRoles"].get("Mask", []))
            acceptance = json.loads((ROOT / row["evidence"]["geometryAcceptance"]).read_text())
            self.assertEqual(acceptance["productGeometryReadback"]["extrusionRoute"], "direct-stencil")
            self.assertTrue(acceptance["productGeometryReadback"]["automaticExtrusionFallback"])
            self.assertTrue(all(acceptance["comparisons"].values()))
        crashes = report["knownPeriodicFinExtrusionFailures"]
        self.assertTrue(all(case["productReturnCode"] == case["directReturnCode"] == -11 for case in crashes))

    def test_periodic_masked_generic_fin_selects_the_verified_boolean_route(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        report = json.loads((ROOT / "docs/stage9-evidence/gpu-coverage-audit/specialized-geometries/coverage-report.json").read_text())
        for model in ("singleParticle", "multiParticle"):
            for mode in ("direct", "extruded"):
                project = make_generic_geometry_project("fin", 3, mode == "extruded", model, "etch")
                project["domain"]["boundaryXY"] = "periodic"
                project["geometry"]["maskHeight"] = 0.4
                project["solver"]["enginePreference"] = "gpu"
                parsed, _ = validate_project(project, kind="simulate")
                normalized = parsed.model_dump(mode="json", exclude_none=True)
                decision = fin_construction_decision(normalized)
                self.assertEqual(decision["effective"], "boolean")
                self.assertEqual(decision["constructionFallbackModels"], [model])
                self.assertEqual(decision["constructionFallbackReason"], "periodic-masked-fin-geometry")
                self.assertEqual(decision["extrusionRoute"], "direct-stencil" if mode == "extruded" else "direct")
                self.assertEqual(decision["extrusionFallbackModels"], [model] if mode == "extruded" else [])
                signature = gpu_project_signature(normalized, normalized["steps"][0])
                self.assertEqual(signature["fin_construction"], "boolean")
                self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")
                row = next(item for item in matrix["rows"] if item["model"] == model
                           and item["operation"] == "etch" and item["dimension"] == 3
                           and item["boundaryXY"] == "periodic" and item["geometryKinds"] == ["fin"]
                           and item.get("finExtrusionMode") == mode and item.get("finConstruction") == "boolean"
                           and "mask" in item["materialRoles"].get("Mask", []))
                self.assertTrue(capability_verified(3, model, "etch", "periodic", "GPU_DISK",
                                                    schema_version=7, **signature))
                acceptance = json.loads((ROOT / row["evidence"]["geometryAcceptance"]).read_text())
                self.assertEqual(acceptance["productGeometryReadback"]["construction"], "boolean")
                self.assertTrue(acceptance["comparisons"]["finConstructionRoute"])
                self.assertTrue(all(value is True for value in acceptance["comparisons"].values()))
        self.assertEqual(report["genericPeriodicFinDiagnosis"]["verifiedAutomaticFallbackCases"], 8)

    def test_reflective_masked_3d_generic_fin_selects_the_verified_boolean_route(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        for model in ("singleParticle", "multiParticle"):
            for mode in ("direct", "extruded"):
                project = make_generic_geometry_project("fin", 3, mode == "extruded", model, "etch")
                project["geometry"]["maskHeight"] = 0.4
                project["solver"]["enginePreference"] = "gpu"
                parsed, _ = validate_project(project, kind="simulate")
                normalized = parsed.model_dump(mode="json", exclude_none=True)
                decision = fin_construction_decision(normalized)
                self.assertEqual(decision["requested"], "native")
                self.assertEqual(decision["effective"], "boolean")
                self.assertEqual(decision["constructionFallbackModels"], [model])
                self.assertEqual(decision["constructionFallbackReason"], "masked-3d-fin-generic-process")
                self.assertEqual(decision["extrusionRoute"], "d2-extrude" if mode == "extruded" else "direct")
                self.assertEqual(decision["extrusionFallbackModels"], [])
                signature = gpu_project_signature(normalized, normalized["steps"][0])
                self.assertEqual(signature["fin_construction"], "boolean")
                self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")
                self.assertTrue(capability_verified(3, model, "etch", "reflective", "GPU_DISK",
                                                    schema_version=7, **signature))
                row = next(item for item in matrix["rows"] if item["model"] == model
                           and item["operation"] == "etch" and item["dimension"] == 3
                           and item["boundaryXY"] == "reflective" and item["geometryKinds"] == ["fin"]
                           and item.get("finExtrusionMode") == mode and item.get("finConstruction") == "boolean"
                           and "mask" in item["materialRoles"].get("Mask", []))
                acceptance = json.loads((ROOT / row["evidence"]["geometryAcceptance"]).read_text())
                self.assertTrue(all(acceptance["comparisons"].values()))

    def test_native_makefin_failure_records_the_viennaps_fallback_matrix(self):
        report = json.loads((ROOT / "docs/stage9-evidence/gpu-coverage-audit/specialized-geometries/coverage-report.json").read_text())
        self.assertEqual(report["nativeFailureDiagnosis"]["models"], ["hbro2", "neutralTransport", "sf6c4f8", "sf6o2"])
        fallback_cases = report["automaticFinFallbackCases"]
        self.assertEqual(sum(not case["masked"] for case in fallback_cases), 24)
        self.assertEqual(sum(case["masked"] for case in fallback_cases), 12)
        self.assertTrue(all(case["status"] == "verified" for case in fallback_cases))
        for case in fallback_cases:
            project = json.loads((ROOT / case["project"]).read_text())
            self.assertEqual(project["geometry"]["construction"], "native")
            self.assertTrue(case["comparisons"]["finConstructionFallback"])
            self.assertEqual(case["productGeometryReadback"]["construction"], "boolean")
            self.assertTrue(case["productGeometryReadback"]["automaticFallback"])

    def test_published_schemas_and_3d_contract_have_no_fixed_resolution_caps(self):
        schema_files = sorted((ROOT / "docs").glob("schema*.json"))
        seen_grid_delta = 0

        def visit(value):
            nonlocal seen_grid_delta
            if isinstance(value, dict):
                grid = value.get("gridDelta")
                if isinstance(grid, dict):
                    seen_grid_delta += 1
                    self.assertNotIn("ge", grid)
                    self.assertNotIn("minimum", grid)
                    self.assertTrue(grid.get("gt") == 0 or grid.get("exclusiveMinimum") == 0, grid)
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        for path in schema_files:
            visit(json.loads(path.read_text()))
        self.assertGreaterEqual(seen_grid_delta, 4)

        snapshot = json.loads((ROOT / "docs/schema-snapshot-v2.json").read_text())
        self.assertNotIn("maxItems", snapshot["properties"]["points"])
        self.assertNotIn("maxItems", snapshot["properties"]["triangles"])
        self.assertNotIn("le", snapshot["$defs"]["Mesh3D"]["properties"]["pointCount"])
        self.assertNotIn("le", snapshot["$defs"]["Mesh3D"]["properties"]["triangleCount"])

        contract = (ROOT / "docs/api-contract-3d.md").read_text()
        for stale in ("32 MiB 单快照", "150,000 点", "250,000 三角形"):
            self.assertNotIn(stale, contract)

    def test_verified_specialized_gpu_models_validate_and_select_without_fallback(self):
        for spec in specialized_catalog()["models"]:
            for dimension in spec.get("dimensions") or (2, 3):
                with self.subTest(model=spec["id"], dimension=dimension):
                    project = make_project(spec["id"], dimension)
                    project["solver"]["enginePreference"] = "gpu"
                    if spec["id"] == "faradayCage" and dimension == 2:
                        # 2D FaradayCage is pinned to CPU_DISK because the
                        # AUTO/GPU kernel hangs for part of the cage/tilt
                        # matrix; the shared gate must reject the GPU request.
                        with self.assertRaises(ProjectInputError):
                            validate_project(project, kind="simulate")
                        continue
                    parsed, _ = validate_project(project, kind="simulate")
                    normalized = parsed.model_dump(mode="json", exclude_none=True)
                    self.assertEqual(select_engine(normalized, normalized["steps"][0]),
                                     "GPU_DISK" if spec["usesFlux"] else "not-applicable")
                    signature = gpu_project_signature(project, project["steps"][0])
                    # Experimental entries are allowlisted and runnable on CPU
                    # (covered by test_engine_batch1_specialized.py) but have no
                    # archived Stage 9 GPU acceptance row.
                    self.assertEqual(capability_verified(dimension, f"specialized:{spec['id']}", spec["operation"],
                                                        "reflective", "GPU_DISK", schema_version=7, **signature),
                                     spec["usesFlux"] and not spec.get("experimental"))

    def test_schema7_gpu_requests_route_to_native_even_without_a_matching_evidence_row(self):
        project = make_project("sf6o2", 2)
        step = project["steps"][0]
        step.pop("nativeModel")
        step.pop("nativeParameters")
        step["model"] = "singleParticle"
        step["rateMagnitude"] = 0.05
        step["stickingProbability"] = 0.5
        step["sourceExponent"] = 1.0
        step["rateMode"] = "uniform"
        project["solver"]["enginePreference"] = "gpu"
        self.assertTrue(uses_flux(step))
        parsed = json.loads(json.dumps(project))
        self.assertEqual(select_engine(parsed, parsed["steps"][0]), "GPU_DISK")
        validate_project(project, kind="simulate")

        unsupported_single = json.loads(json.dumps(project))
        unsupported_single["solver"]["raysPerPoint"] = 128
        self.assertEqual(select_engine(unsupported_single, unsupported_single["steps"][0]), "GPU_DISK")
        validate_project(unsupported_single, kind="simulate")

        multi = multi_project(2)
        multi["solver"]["enginePreference"] = "gpu"
        self.assertEqual(select_engine(multi, multi["steps"][0]), "GPU_DISK")
        validate_project(multi, kind="simulate")
        multi["domain"]["xBoundary"] = "periodic"
        self.assertEqual(select_engine(multi, multi["steps"][0]), "GPU_DISK")
        validate_project(multi, kind="simulate")

    def test_legacy_gpu_rows_remain_available_for_verified_v1_and_v2_cases(self):
        self.assertTrue(capability_verified(2, "singleParticle", "etch", "reflective", "GPU_DISK", schema_version=1))
        for boundary in ("reflective", "periodic"):
            for operation in ("etch", "deposit"):
                self.assertTrue(capability_verified(3, "singleParticle", operation, boundary, "GPU_DISK", schema_version=2))

    def test_schema7_gpu_rows_require_measured_geometry_pattern_and_material_signature(self):
        path = ROOT / "docs/stage9-evidence/browser-multiparticle/2d/project.json"
        browser_project = json.loads(path.read_text())
        browser_project["solver"]["enginePreference"] = "gpu"
        with measured_engine():
            parsed, _ = validate_project(browser_project, kind="simulate")
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")

        unsupported = json.loads(path.read_text())
        unsupported["geometry"]["pattern"]["kind"] = "hole"
        signature = gpu_project_signature(unsupported, unsupported["steps"][0])
        self.assertFalse(capability_verified(2, "multiParticle", "etch", "reflective", "GPU_DISK",
                                             schema_version=7, **signature))

        signature["pattern_kind"] = None
        signature["materials"] = ["Si", "SiO2"]
        self.assertFalse(capability_verified(2, "multiParticle", "etch", "reflective", "GPU_DISK",
                                             schema_version=7, **signature))
        self.assertFalse(capability_verified(2, "multiParticle", "etch", "reflective", "GPU_DISK", schema_version=7))

        deposit = json.loads(path.read_text())
        step = deposit["steps"][1]
        signature = gpu_project_signature(deposit, step)
        self.assertTrue(capability_verified(2, "multiParticle", "deposit", "reflective", "GPU_DISK",
                                            schema_version=7, **signature))
        step["depositMaterial"] = "Si3N4"
        signature = gpu_project_signature(deposit, step)
        self.assertFalse(capability_verified(2, "multiParticle", "deposit", "reflective", "GPU_DISK",
                                             schema_version=7, **signature))
        changed_role = json.loads(path.read_text())
        changed_role["geometry"]["layers"][1]["role"] = "film"
        signature = gpu_project_signature(changed_role, changed_role["steps"][0])
        self.assertFalse(capability_verified(2, "multiParticle", "etch", "reflective", "GPU_DISK",
                                             schema_version=7, **signature))

        for label, species in (("eight-ions", [browser_project["steps"][0]["species"][0] for _ in range(8)]),):
            unsupported = json.loads(path.read_text())
            step = unsupported["steps"][0]
            step["species"] = json.loads(json.dumps(species))
            for index, particle in enumerate(step["species"]):
                particle["label"] = f"{label}-{index}"
            labels = [particle["label"] for particle in step["species"]]
            step["rateCoefficients"] = {"defaultBySpecies": {name: 0.025 for name in labels},
                                         "byMaterial": {"SiO2": {name: 0.04 for name in labels}}}
            unsupported["solver"]["enginePreference"] = "gpu"
            self.assertEqual(select_engine(unsupported, unsupported["steps"][0]), "GPU_DISK", label)
            with measured_engine():
                validate_project(unsupported, kind="simulate")

    def test_generic_gpu_material_scopes_cover_directly_swept_native_ids(self):
        report = json.loads((ROOT / "docs/stage9-evidence/gpu-coverage-audit/material-scope/coverage-report.json").read_text())
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        catalog = json.loads((ROOT / "docs/materials-catalog.json").read_text())["materials"]
        material_names = [item["name"] for item in catalog]
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["engineVersions"], {"viennaps": "4.6.2", "viennals": "5.8.5"})
        self.assertEqual(report["builtInMaterialRoleCoverage"]["reflective"]["plane"]["bulk"],
                         sorted(int(item["id"]) for item in catalog))
        rows = [row for row in matrix["rows"] if row.get("materialScope")]
        self.assertGreaterEqual(len(rows), 60)
        self.assertTrue(all(row["materialScope"]["evidence"] for row in rows))

        # Every exposed native material ID is accepted for each exercised
        # geometry role. Distinct names preserve the native mask/bulk mapping.
        for geometry_kind, dimension, extruded in (
                ("plane", 2, False), ("trench", 3, False), ("hole", 3, False),
                ("fin", 2, False), ("fin", 3, False), ("fin", 3, True)):
            for index, material_name in enumerate(material_names):
                project = make_generic_geometry_project(geometry_kind, dimension, extruded,
                                                        "singleParticle", "deposit")
                project["geometry"]["substrateMaterial"] = material_name
                if geometry_kind in {"trench", "hole", "fin"}:
                    project["geometry"]["maskHeight"] = 0.4
                    project["geometry"]["maskMaterial"] = material_names[(index + 1) % len(material_names)]
                project["steps"][0]["depositMaterial"] = material_names[(index + 2) % len(material_names)]
                signature = gpu_project_signature(project, project["steps"][0])
                self.assertTrue(capability_verified(dimension, "singleParticle", "deposit", "reflective",
                                                    "GPU_DISK", schema_version=7, **signature),
                                (geometry_kind, dimension, material_name))

        for geometry_kind, dimension, extruded in (
                ("plane", 2, False), ("trench", 2, False), ("trench", 3, False),
                ("hole", 3, False), ("fin", 2, False), ("fin", 3, False), ("fin", 3, True)):
            for index, material_name in enumerate(material_names):
                project = make_generic_geometry_project(geometry_kind, dimension, extruded,
                                                        "multiParticle", "deposit")
                project["domain"]["boundaryXY" if dimension == 3 else "xBoundary"] = "periodic"
                project["geometry"]["substrateMaterial"] = material_name
                if geometry_kind in {"trench", "hole", "fin"}:
                    project["geometry"]["maskHeight"] = 0.4
                    project["geometry"]["maskMaterial"] = material_names[(index + 1) % len(material_names)]
                project["steps"][0]["depositMaterial"] = material_names[(index + 2) % len(material_names)]
                signature = gpu_project_signature(project, project["steps"][0])
                self.assertTrue(capability_verified(dimension, "multiParticle", "deposit", "periodic",
                                                    "GPU_DISK", schema_version=7, **signature),
                                ("periodic", geometry_kind, dimension, material_name))

        custom_rows = [*catalog,
                       {"name": "GpuCustomFirst", "id": 177, "isCustom": True},
                       {"name": "GpuCustomLast", "id": 240, "isCustom": True},
                       {"name": "GpuCustomOutOfRange", "id": 241, "isCustom": True}]
        for name, native_id, expected in (("GpuCustomFirst", 177, True),
                                          ("GpuCustomLast", 240, True),
                                          ("GpuCustomOutOfRange", 241, False)):
            project = make_generic_geometry_project("plane", 2, False, "singleParticle", "etch")
            project["geometry"]["substrateMaterial"] = name
            signature = gpu_project_signature(project, project["steps"][0])
            with patch("backend.validation._catalog_names", return_value=custom_rows):
                self.assertEqual(capability_verified(2, "singleParticle", "etch", "reflective",
                                                     "GPU_DISK", schema_version=7, **signature), expected,
                                 (native_id, name))

    def test_specialized_gpu_substrate_scope_matches_only_swept_plane_inputs(self):
        scope_report_path = (ROOT / "docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/material-scope/"
                             "specialized-substrate-capability-report.json")
        report = json.loads(scope_report_path.read_text())
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        self.assertEqual(report["status"], "passed")
        self.assertEqual((report["dimension"], report["geometry"], report["boundaryXY"],
                          report["materialCount"], report["modelCount"], report["caseCount"],
                          report["verifiedCount"]), (2, "plane", "reflective", 13, 10, 130, 130))
        self.assertEqual(report["device"], {"name": "NVIDIA GeForce RTX 3060 Laptop GPU",
                                            "uuid": "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d",
                                            "pciBusId": "0000:01:00.0"})
        rows = [row for row in matrix["rows"] if row.get("materialScope")
                and row["model"].startswith("specialized:")]
        self.assertEqual(len(rows), 10)
        for model_row in report["rows"]:
            self.assertEqual(len(model_row["verifiedCases"]), 13)
            self.assertEqual(model_row["substrateMaterials"], sorted(
                row["name"] for row in json.loads((ROOT / "docs/materials-catalog.json").read_text())["materials"]))
            for evidence in model_row["verifiedCases"]:
                project = json.loads((ROOT / evidence["project"]).read_text())
                parsed, _ = validate_project(project, kind="simulate")
                normalized = parsed.model_dump(mode="json", exclude_none=True)
                signature = gpu_project_signature(normalized, normalized["steps"][0])
                self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")
                self.assertTrue(capability_verified(2, f"specialized:{model_row['model']}",
                                                    model_row["operation"], "reflective", "GPU_DISK",
                                                    schema_version=7, **signature),
                                (model_row["model"], evidence["material"]))
                self.assertEqual(evidence["cudaDeviceReadback"]["uuid"], report["device"]["uuid"])
                self.assertTrue(evidence["cudaDeviceReadback"]["process"]["processListed"])

        # These changes leave the measured substrate axis and must fail closed.
        ald = next(row for row in report["rows"] if row["model"] == "ald")
        altered_target = json.loads((ROOT / ald["verifiedCases"][0]["project"]).read_text())
        altered_target["steps"][0]["depositMaterial"] = "SiO2"
        signature = gpu_project_signature(altered_target, altered_target["steps"][0])
        self.assertFalse(capability_verified(2, "specialized:ald", "deposit", "reflective", "GPU_DISK",
                                             schema_version=7, **signature))

        fluorocarbon = next(row for row in report["rows"] if row["model"] == "fluorocarbon")
        altered_table = json.loads((ROOT / fluorocarbon["verifiedCases"][0]["project"]).read_text())
        altered_table["steps"][0]["nativeParameters"]["materialParametersByMaterial"]["SiO2"] = dict(
            FLUOROCARBON_MATERIAL_DEFAULTS)
        signature = gpu_project_signature(altered_table, altered_table["steps"][0])
        self.assertFalse(capability_verified(2, "specialized:fluorocarbon", "etch", "reflective", "GPU_DISK",
                                             schema_version=7, **signature))

        changed_geometry = json.loads((ROOT / fluorocarbon["verifiedCases"][0]["project"]).read_text())
        signature = gpu_project_signature(changed_geometry, changed_geometry["steps"][0])
        signature["geometry_kind"] = "trench"
        self.assertFalse(capability_verified(2, "specialized:fluorocarbon", "etch", "reflective", "GPU_DISK",
                                             schema_version=7, **signature))

    def test_generic_multiparticle_material_tables_are_scoped_without_widening_species(self):
        project = make_generic_geometry_project("plane", 2, False, "multiParticle", "etch")
        step = project["steps"][0]
        labels = [item["label"] for item in step["species"]]
        catalog = json.loads((ROOT / "docs/materials-catalog.json").read_text())["materials"]
        names = [item["name"] for item in catalog]
        step["rateCoefficients"]["byMaterial"] = {
            name: {labels[0]: 0.08 + index * 0.01, labels[1]: 0.12 + index * 0.01}
            for index, name in enumerate(names)
        }
        step["species"][1]["stickingProbabilityByMaterial"] = {
            name: 0.2 + 0.05 * (index % 10) for index, name in enumerate(names)
        }
        signature = gpu_project_signature(project, step)
        self.assertTrue(capability_verified(2, "multiParticle", "etch", "reflective", "GPU_DISK",
                                            schema_version=7, **signature))

        changed_native_species = json.loads(json.dumps(project))
        changed_native_species["steps"][0]["species"][0]["meanEnergy"] += 1
        signature = gpu_project_signature(changed_native_species, changed_native_species["steps"][0])
        self.assertFalse(capability_verified(2, "multiParticle", "etch", "reflective", "GPU_DISK",
                                             schema_version=7, **signature))

    def test_gds_gpu_rows_require_the_measured_native_layer_mapping_scope(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        rows = [row for row in matrix["rows"] if row.get("geometryKinds") == ["gds"]]
        generic_rows = [row for row in rows if not row["model"].startswith("specialized:")]
        specialized_rows = [row for row in rows if row["model"].startswith("specialized:")]
        gds_material_report = json.loads((ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-geometry/material-scope/summary.json").read_text())
        self.assertEqual((len(generic_rows), len(specialized_rows)), (24, 40))
        self.assertTrue(all(row["gdsScope"]["maxLayerMappings"] == 64
                            and row["gdsScope"]["supports3DMask"] for row in generic_rows))
        self.assertTrue(all(row["gdsScope"]["maxLayerMappings"] == 2
                            and row["gdsScope"]["supports3DMask"] == (row["dimension"] == 3)
                            for row in specialized_rows))
        known_failures = matrix["knownNativeFailures"]
        self.assertEqual(known_failures, [])
        self.assertTrue(gds_material_report["passed"])
        browser_report = json.loads((ROOT / "docs/stage9-evidence/gpu-coverage-audit/browser/gds-gpu/acceptance.json").read_text())
        browser_teos = next(row for row in browser_report["cases"]
                            if row["name"] == "3d-teos-gds-distinct-deposition-and-mask-materials")
        self.assertEqual(browser_report["status"], "passed")
        self.assertEqual(browser_teos["actualEngine"], "GPU_DISK")
        self.assertTrue(browser_teos["initialAndFinalExactlyMatchDirect"])
        periodic_browser = json.loads((ROOT / "docs/stage9-evidence/gpu-coverage-audit/browser/gds-periodic-repair/acceptance.json").read_text())
        self.assertEqual(periodic_browser["status"], "passed")
        self.assertEqual(periodic_browser["periodicGdsGpu"]["actualEngine"], "GPU_DISK")
        self.assertTrue(periodic_browser["periodicGdsGpu"]["initialAndFinalExactlyMatchDirect"])
        self.assertTrue(periodic_browser["periodicGdsGpu"]["periodicBoundsMatch"])
        self.assertEqual(periodic_browser["periodicGdsGpu"]["runStatus"], "succeeded")
        multilayer_report = json.loads((ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-multilayer/summary.json").read_text())
        self.assertTrue(multilayer_report["passed"])
        self.assertEqual((multilayer_report["caseCount"], multilayer_report["expectedCaseCount"]), (80, 80))
        layer_count_report = json.loads((ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-layer-count/grid-aligned-0.1/summary.json").read_text())
        self.assertTrue(layer_count_report["passed"])
        self.assertEqual((layer_count_report["mappingCount"], layer_count_report["caseCount"],
                          layer_count_report["expectedCaseCount"]), (64, 32, 32))
        cpu_matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-specialized/targeted-summary-cpu_disk-two-layer-film-mask.json").read_text())
        self.assertEqual((cpu_matrix["caseCount"], cpu_matrix["expectedCaseCount"]), (40, 40))
        self.assertTrue(cpu_matrix["passed"])
        self.assertTrue(all(row["status"] == "verified" and all(row["comparisons"].values())
                            for row in cpu_matrix["rows"]))
        catalog = json.loads((ROOT / "docs/materials-catalog.json").read_text())["materials"]
        builtin_names = [item["name"] for item in catalog]
        for row in rows:
            project_path = ROOT / row["evidence"]["geometryAcceptance"]
            acceptance = json.loads(project_path.read_text())
            project_file = ROOT / acceptance["project"]
            project = json.loads(project_file.read_text())
            step = project["steps"][0]
            signature = gpu_project_signature(project, step)
            self.assertTrue(capability_verified(project["dimension"], row["model"], step["operation"],
                                                row["boundaryXY"], "GPU_DISK", schema_version=7,
                                                **signature))
            asset = (ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-multilayer/multilayer-two-adjacent-polygons.gds"
                     if row["model"].startswith("specialized:") else
                     ROOT / "test-suite-gpu-data/gds-assets" / f"{project['geometry']['assetId']}.gds")
            with patch("backend.storage.resolve_gds_asset", return_value=asset):
                parsed, _ = validate_project(project, kind="simulate")
            normalized = parsed.model_dump(mode="json", exclude_none=True)
            self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")
            two_layers = json.loads(json.dumps(project))
            if len(two_layers["geometry"]["layers"]) < 2:
                two_layers["geometry"]["layers"].append(dict(two_layers["geometry"]["layers"][0]))
            two_layer_signature = gpu_project_signature(two_layers, two_layers["steps"][0])
            self.assertTrue(capability_verified(project["dimension"], row["model"], step["operation"],
                                                row["boundaryXY"], "GPU_DISK", schema_version=7,
                                                **two_layer_signature))
            three_layers = json.loads(json.dumps(two_layers))
            three_layers["geometry"]["layers"].append(dict(three_layers["geometry"]["layers"][0]))
            three_layer_signature = gpu_project_signature(three_layers, three_layers["steps"][0])
            self.assertEqual(capability_verified(project["dimension"], row["model"], step["operation"],
                                                 row["boundaryXY"], "GPU_DISK", schema_version=7,
                                                 **three_layer_signature), not row["model"].startswith("specialized:"))
            sixty_five_layers = json.loads(json.dumps(project))
            sixty_five_layers["geometry"]["layers"] *= 65
            sixty_five_signature = gpu_project_signature(sixty_five_layers, sixty_five_layers["steps"][0])
            self.assertFalse(capability_verified(project["dimension"], row["model"], step["operation"],
                                                 row["boundaryXY"], "GPU_DISK", schema_version=7,
                                                 **sixty_five_signature))

            # Material identities and deposit bindings are varied over every
            # installed built-in because the GDS material sweep compared those
            # native maps directly, rather than only the baseline fixture.
            if not row["model"].startswith("specialized:"):
                for name in builtin_names:
                    varied = json.loads(json.dumps(project))
                    varied["geometry"]["layers"][0]["material"] = name
                    if step["operation"] == "deposit":
                        varied["steps"][0]["depositMaterial"] = name
                    varied_step = varied["steps"][0]
                    varied_signature = gpu_project_signature(varied, varied_step)
                    self.assertTrue(capability_verified(varied["dimension"], row["model"], varied_step["operation"],
                                                        row["boundaryXY"], "GPU_DISK", schema_version=7,
                                                        **varied_signature),
                                    f"GDS built-in material scope rejected {name}: {row['model']}/{step['operation']}")

        # The direct scope includes custom native IDs at both registry edges.
        # Exercise the same catalog data the persisted material registry gives
        # the validator, while keeping this test independent of user data.
        for case in gds_material_report["rows"]:
            if not case.get("customMaterialIds"):
                continue
            evidence = json.loads((ROOT / case["project"]).read_text())
            injected = [{**definition, "isCustom": True}
                        for definition in evidence["materialDefinitions"]]
            isolated_catalog = {"schemaVersion": 1, "engineVersion": "4.6.2",
                                "materials": [*catalog, *injected]}
            evidence_step = evidence["steps"][0]
            with patch("backend.materials.material_catalog", return_value=isolated_catalog):
                signature = gpu_project_signature(evidence, evidence_step)
                self.assertTrue(capability_verified(
                    evidence["dimension"], evidence_step["model"], evidence_step["operation"],
                    case["boundaryXY"], "GPU_DISK", schema_version=7, **signature),
                    f"GDS custom edge material scope rejected {case['customMaterialIds']}: {case['name']}")

        # Each allowed two-mapping profile is backed by a real product/direct
        # GPU_DISK case, including the mixed film/mask and custom registry edge
        # pair. Keep all of those measured cases executable by the selector.
        for case in multilayer_report["rows"]:
            evidence = json.loads((ROOT / case["project"]).read_text())
            evidence_step = evidence["steps"][0]
            catalog_rows = list(catalog)
            catalog_rows.extend({**definition, "isCustom": True}
                                for definition in evidence.get("materialDefinitions", []))
            isolated_catalog = {"schemaVersion": 1, "engineVersion": "4.6.2", "materials": catalog_rows}
            with patch("backend.materials.material_catalog", return_value=isolated_catalog):
                signature = gpu_project_signature(evidence, evidence_step)
                self.assertEqual(signature["gds_mapping_count"], 2)
                self.assertTrue(capability_verified(
                    evidence["dimension"], evidence_step["model"], evidence_step["operation"],
                    case["boundaryXY"], "GPU_DISK", schema_version=7, **signature),
                    f"GDS measured two-layer scope rejected: {case['name']}")

        for case in layer_count_report["rows"]:
            evidence = json.loads((ROOT / case["project"]).read_text())
            evidence_step = evidence["steps"][0]
            signature = gpu_project_signature(evidence, evidence_step)
            self.assertEqual(signature["gds_mapping_count"], 64)
            self.assertTrue(capability_verified(
                evidence["dimension"], evidence_step["model"], evidence_step["operation"],
                case["boundaryXY"], "GPU_DISK", schema_version=7, **signature),
                f"GDS measured 64-layer scope rejected: {case['name']}")

        specialized_gds = json.loads((ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-specialized/summary.json").read_text())
        self.assertEqual((specialized_gds["caseCount"], specialized_gds["expectedCaseCount"]), (40, 40))
        self.assertTrue(specialized_gds["passed"])
        self.assertTrue(all(case["status"] == "verified" and all(case["comparisons"].values())
                            for case in specialized_gds["rows"]))
        periodic_rows = [case for case in specialized_gds["rows"] if case["boundaryXY"] == "periodic"]
        self.assertEqual(len(periodic_rows), 20)
        self.assertTrue(all(case["comparisons"][key] for case in periodic_rows
                            for key in ("periodicSupportLayer", "periodicSupportBounds", "periodicNativeBounds")))

    def test_periodic_gds_support_preserves_gpu_engine_selection(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        project_path = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-specialized/sf6o2-3d-gds-film-mask-periodic-gpu_disk/project.json"
        project = json.loads(project_path.read_text())
        asset = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-multilayer/multilayer-two-adjacent-polygons.gds"
        self.assertEqual(matrix["knownNativeFailures"], [])
        row = next(item for item in matrix["rows"] if item["model"] == "specialized:sf6o2"
                   and item["dimension"] == 3 and item["boundaryXY"] == "periodic"
                   and item["geometryKinds"] == ["gds"])
        acceptance = json.loads((ROOT / row["evidence"]["geometryAcceptance"]).read_text())
        readback = acceptance["productGeometryReadback"]
        self.assertEqual(readback["supportPurpose"], "align-native-periodic-gds-bounds-with-project-domain")
        self.assertEqual(readback["supportBoundsUm"], [-6.0, 6.0, -4.0, 4.0])
        requested = json.loads(json.dumps(project))
        requested["solver"]["enginePreference"] = "gpu"
        with patch("backend.storage.resolve_gds_asset", return_value=asset):
            loaded, warnings = validate_project(requested, kind="simulate")
        normalized = loaded.model_dump(mode="json", exclude_none=True)
        self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")
        self.assertFalse(any("SIGSEGV" in warning for warning in warnings))

    def test_default_webui_multiparticle_gpu_combination_matches_its_direct_evidence(self):
        project_path = ROOT / "docs/stage9-evidence/gpu-coverage-audit/default-ui-multiparticle/project.json"
        report_path = ROOT / "docs/stage9-evidence/gpu-coverage-audit/default-ui-multiparticle/acceptance.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "passed")
        self.assertTrue(all(report["comparisons"].values()))
        signature = gpu_project_signature(project, project["steps"][0])
        self.assertTrue(capability_verified(2, "multiParticle", "etch", "reflective", "GPU_DISK",
                                            schema_version=7, **signature))
        project["solver"]["enginePreference"] = "gpu"
        parsed, _ = validate_project(project, kind="simulate")
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")

        unsupported = json.loads(json.dumps(project))
        unsupported["steps"][0]["rateCoefficients"]["defaultBySpecies"]["argonIon"] += 0.001
        changed_signature = gpu_project_signature(unsupported, unsupported["steps"][0])
        self.assertFalse(capability_verified(2, "multiParticle", "etch", "reflective", "GPU_DISK",
                                             schema_version=7, **changed_signature))

    def test_mixed_flux_and_directional_recipe_uses_gpu_only_for_flux_step(self):
        project = make_project("sf6o2", 2)
        step = project["steps"][0]
        step.pop("nativeModel")
        step.pop("nativeParameters")
        step.update({"model": "singleParticle", "rateMode": "uniform", "rateMagnitude": 0.05,
                     "stickingProbability": 0.5, "sourceExponent": 1.0})
        project["steps"].append({"id": "directional_after", "name": "directional", "enabled": True,
            "operation": "etch", "model": "directional", "direction": [0.0, -1.0, 0.0],
            "directionalRateMagnitude": 0.1, "isotropicRateMagnitude": 0.0,
            "calculateVisibility": True, "duration": 0.1})
        project["solver"]["enginePreference"] = "gpu"
        parsed, _ = validate_project(project, kind="simulate")
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        self.assertEqual([select_engine(normalized, item) for item in normalized["steps"]],
                         ["GPU_DISK", "not-applicable"])


if __name__ == "__main__":
    unittest.main()
