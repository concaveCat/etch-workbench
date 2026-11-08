"""Publish only geometry/material-scoped GPU tuples with direct references."""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.native_build import fingerprint_package
from backend.validation import gpu_project_signature
from backend.specialized_models import specialized_catalog

SPECIALIZED = ROOT / "docs/stage9-evidence/gpu-specialized/summary-gpu_disk.json"
MULTIPARTICLE = ROOT / "docs/stage9-evidence/gpu-multiparticle/summary.json"
BROWSER_MULTI = ROOT / "docs/stage9-evidence/browser-multiparticle/browser-live.json"
BROWSER_SF6 = ROOT / "docs/stage9-evidence/browser/browser-gpu-live.json"
SINGLEPARTICLE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/singleparticle/summary.json"
NEUTRAL_MULTIPARTICLE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-neutral/summary.json"
DEFAULT_UI_MULTIPARTICLE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/default-ui-multiparticle/acceptance.json"
GEOMETRY_PRESETS = ROOT / "docs/stage9-evidence/gpu-coverage-audit/geometry-presets/summary.json"
GDS_GEOMETRY = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-geometry/summary.json"
GDS_MATERIAL_SCOPE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-geometry/material-scope/summary.json"
GDS_MULTILAYER = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-multilayer/summary.json"
GDS_LAYER_COUNT = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-layer-count/grid-aligned-0.1/summary.json"
GDS_SPECIALIZED = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-specialized/summary.json"
GDS_SPECIALIZED_BROWSER = ROOT / "docs/stage9-evidence/gpu-coverage-audit/browser/gds-gpu/acceptance.json"
GDS_PERIODIC_BROWSER = ROOT / "docs/stage9-evidence/gpu-coverage-audit/browser/gds-periodic-repair/acceptance.json"
GDS_NATIVE_FAILURE_BROWSER = ROOT / "docs/stage9-evidence/gpu-coverage-audit/browser/gds-native-failure-gate/acceptance.json"
GDS_SPECIALIZED_SINGLE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-specialized/targeted-summary-gpu_disk-single-layer.json"
GDS_SPECIALIZED_OFFSET = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-specialized/targeted-summary-gpu_disk-single-layer-offset.json"
GDS_SPECIALIZED_MASK = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-specialized/targeted-summary-gpu_disk-single-mask.json"
GDS_SPECIALIZED_CPU = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-specialized/targeted-summary-cpu_disk.json"
GDS_SPECIALIZED_CPU_MATRIX = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-specialized/targeted-summary-cpu_disk-two-layer-film-mask.json"
GDS_SPECIALIZED_GDB = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-specialized/sf6o2-2d-gds-film-mask-quarter-grid-offset-periodic-gpu_disk/gdb.log"
MULTIPARTICLE_GPU_SPECIES = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/summary.json"
MULTIPARTICLE_GPU_WARNING = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-neutral/native-edge-notes/2d-eight-ions.log"
MULTIPARTICLE_CPU_ION_MATRIX = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/cpu-ion-diagnostic/summary.json"
MULTIPARTICLE_LIMIT_BROWSER = ROOT / "docs/stage9-evidence/gpu-coverage-audit/browser/multiparticle-ion-limit/acceptance.json"
MULTIPARTICLE_GPU_FLUX = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/cpu-ion-diagnostic/gpu-2d-two-ion-flux.json"
MULTIPARTICLE_CPU_FLUX = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/cpu-ion-diagnostic/cpu-2d-two-ion-flux.json"
MULTIPARTICLE_GPU_CLASS_ROUTE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/gpu-class-route/summary.json"
MULTIPARTICLE_GPU_CLASS_ROUTE_GUARD = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/gpu-class-route/context-route-runner.guard.json"
MULTIPARTICLE_SPLIT_FLUX_SUMMARY = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/single-pass-flux-species/summary.json"
MULTIPARTICLE_SPLIT_GEOMETRY_SUMMARY = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/geometry-scope/summary.json"
MULTIPARTICLE_PATCHED_EIGHT_ION = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/eight-ion-direct-comparison.json"
MULTIPARTICLE_PATCHED_UI_2D = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/eight-ion-ui-direct-2d-comparison.json"
MULTIPARTICLE_PATCHED_UI_3D = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/eight-ion-ui-direct-3d-comparison.json"
MULTIPARTICLE_PATCHED_BROWSER_2D = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/browser-ui-acceptance-eight-ions.json"
MULTIPARTICLE_PATCHED_BROWSER_3D = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/browser-ui-acceptance-3d-eight-ions.json"
MULTIPARTICLE_PATCHED_GDS = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-geometry/multi-ion/summary.json"
MULTIPARTICLE_PATCHED_PROFILE_RECHECK = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/profile-recheck/summary.json"
MULTIPARTICLE_PATCHED_PROFILE_BROWSER = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/browser-ui-acceptance-profile-recheck.json"
MULTIPARTICLE_PATCHED_CPU_GPU_PROFILE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/profile-recheck/cpu-gpu-profile-comparison.json"
MULTIPARTICLE_FLUX_GUARDS = [
    ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/cpu-ion-diagnostic/gpu-2d-two-ion.guard.json",
    ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/cpu-ion-diagnostic/cpu-2d-two-ion.guard.json",
]
MULTIPARTICLE_NATIVE_API = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/cpu-ion-diagnostic/native-api.log"
MULTIPARTICLE_NATIVE_API_GUARD = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/cpu-ion-diagnostic/native-api.guard.json"
MULTIPARTICLE_VELOCITY_API = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/cpu-ion-diagnostic/velocity-field-api.log"
MULTIPARTICLE_VELOCITY_API_GUARD = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/cpu-ion-diagnostic/velocity-field-api.guard.json"
MULTIPARTICLE_VELOCITY_ASSIGNMENT = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/cpu-ion-diagnostic/velocity-field-assignment.log"
MULTIPARTICLE_VELOCITY_ASSIGNMENT_GUARD = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/cpu-ion-diagnostic/velocity-field-assignment.guard.json"
SPECIALIZED_GEOMETRIES = ROOT / "docs/stage9-evidence/gpu-coverage-audit/specialized-geometries"
GPU_TRIANGLE_VARIANTS = ROOT / "docs/stage9-evidence/gpu-variants"
PORTABLE_RUNS = ROOT / "docs/stage9-evidence/gpu-coverage-audit/legacy-products"
OUTPUT = ROOT / "docs/stage9-evidence/gpu-capability-matrix.json"
GEOMETRY_COVERAGE_REPORT = SPECIALIZED_GEOMETRIES / "coverage-report.json"
MATERIAL_SCOPE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/material-scope"
MATERIAL_SCOPE_REPORT = MATERIAL_SCOPE / "coverage-report.json"
SPECIALIZED_MATERIAL_SCOPE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/material-scope"
ENGINE_VERSIONS = {"viennaps": "4.6.2", "viennals": "5.8.5"}


def material_row(project: dict, step: dict) -> dict:
    signature = gpu_project_signature(project, step)
    result = {
        "geometryKinds": [signature["geometry_kind"]],
        "materials": signature["materials"],
        "materialRoles": signature["material_roles"],
        "materialBindings": signature["material_bindings"],
    }
    if signature["species_signature"] is not None:
        result["speciesSignature"] = signature["species_signature"]
    if signature["pattern_kind"] is not None:
        result["patternKinds"] = [signature["pattern_kind"]]
    if signature["fin_extrusion_mode"] is not None:
        result["finExtrusionMode"] = signature["fin_extrusion_mode"]
        result["finConstruction"] = signature["fin_construction"]
    return result


def gpu_row(project: dict, step: dict, model: str, dimension: int, evidence: dict,
            *, operation: str | None = None, solver_profiled: bool = False) -> dict:
    row = {
        "schemaVersions": [7], "dimension": dimension, "model": model,
        "operation": operation or step["operation"],
        "boundaryXY": project["domain"].get("boundaryXY", project["domain"].get("xBoundary")),
        "engine": "GPU_DISK", "status": "verified", **material_row(project, step),
        "evidence": evidence,
    }
    if solver_profiled:
        row["solverProfile"] = gpu_project_signature(project, step)["solver_profile"]
    return row


def add_browser_multiparticle_rows(rows: list[dict]) -> None:
    report = json.loads(BROWSER_MULTI.read_text(encoding="utf-8"))
    if not report.get("passed") or report.get("engine") != "GPU_DISK" or not all(report.get("exactDirectViennaPSMatch", {}).values()):
        raise SystemExit("MultiParticle Chromium GPU evidence is incomplete")
    for dimension, run_key in ((2, "process2d"), (3, "process3d")):
        input_dir = ROOT / "docs/stage9-evidence/browser-multiparticle" / f"{dimension}d"
        project = json.loads((input_dir / "project.json").read_text(encoding="utf-8"))
        direct = json.loads((input_dir / "direct.json").read_text(encoding="utf-8"))
        run_id = report["runs"][run_key]
        worker_path = PORTABLE_RUNS / "stage9-multiparticle-browser-data/runs" / run_id / "worker-result.json"
        worker = json.loads(worker_path.read_text(encoding="utf-8"))
        if worker.get("versions") != {"viennaps": "4.6.2", "viennals": "5.8.5"}:
            raise SystemExit(f"wrong installed engine versions in browser run {run_id}")
        direct_steps = direct.get("steps", {})
        product_steps = {row["stepId"]: row for row in worker.get("stepRecords", [])}
        for step in project["steps"]:
            if step.get("model") != "multiParticle" or not any(
                float(v) > 0 for v in step.get("rateCoefficients", {}).get("defaultBySpecies", {}).values()
            ) and not any(float(v) > 0 for row in step.get("rateCoefficients", {}).get("byMaterial", {}).values() for v in row.values()):
                continue
            native = direct_steps.get(step["id"], {})
            product = product_steps.get(step["id"], {})
            if native.get("engine") != "GPU_DISK" or product.get("actualEngine") != "GPU_DISK" or native.get("noOp"):
                raise SystemExit(f"browser MultiParticle case did not execute on GPU: {dimension}D/{step['operation']}")
            rows.append(gpu_row(project, step, "multiParticle", dimension, {
                "productWorker": str(worker_path.relative_to(ROOT)),
                "productGuard": str((worker_path.parent / "child-record.json").relative_to(ROOT)),
                "directReference": str((input_dir / "direct.json").relative_to(ROOT)),
                "directGuard": str((input_dir / "direct.guard.json").relative_to(ROOT)),
                "browserReport": str(BROWSER_MULTI.relative_to(ROOT)),
                "comparisons": {"exactDirectViennaPSMatch": True, "actualEngine": "GPU_DISK"},
                "versions": worker["versions"], "seed": worker.get("actualSeed"),
            }))


def add_browser_specialized_row(rows: list[dict]) -> None:
    report = json.loads(BROWSER_SF6.read_text(encoding="utf-8"))
    if report.get("status") != "passed" or report.get("actualEngine") != "GPU_DISK" or not report.get("exactDirectReferenceMatch"):
        raise SystemExit("SF6/O2 Chromium GPU evidence is incomplete")
    run_id = report["runId"]
    stem = ROOT / "docs/stage9-evidence/browser" / f"sf6o2-{run_id}"
    project = json.loads(Path(f"{stem}.project.json").read_text(encoding="utf-8"))
    step = project["steps"][0]
    worker_path = PORTABLE_RUNS / "stage9-browser-data/runs" / run_id / "worker-result.json"
    worker = json.loads(worker_path.read_text(encoding="utf-8"))
    if worker.get("stepRecords", [{}])[0].get("actualEngine") != "GPU_DISK":
        raise SystemExit("SF6/O2 product worker did not record GPU_DISK")
    rows.append(gpu_row(project, step, "specialized:sf6o2", project["dimension"], {
        "productWorker": str(worker_path.relative_to(ROOT)),
        "productGuard": str((worker_path.parent / "child-record.json").relative_to(ROOT)),
        "directReference": f"docs/stage9-evidence/browser/sf6o2-{run_id}.direct.json",
        "directGuard": f"docs/stage9-evidence/browser/sf6o2-{run_id}.direct.guard.json",
        "browserReport": str(BROWSER_SF6.relative_to(ROOT)),
        "comparisons": {"exactDirectViennaPSMatch": True, "actualEngine": "GPU_DISK"},
        "versions": report["versions"], "seed": worker.get("actualSeed"),
    }))


def add_singleparticle_and_neutral_rows(rows: list[dict]) -> None:
    singleparticle = json.loads(SINGLEPARTICLE.read_text(encoding="utf-8"))
    neutral = json.loads(NEUTRAL_MULTIPARTICLE.read_text(encoding="utf-8"))
    for report, model in ((singleparticle, "singleParticle"), (neutral, "multiParticle")):
        if report.get("status") != "passed" or not report.get("passed"):
            raise SystemExit(f"{model} GPU evidence is incomplete")
        for case in report.get("rows", []):
            if case.get("status") != "verified" or case.get("engine") != "GPU_DISK" or not all(case.get("comparisons", {}).values()):
                raise SystemExit(f"unverified {model} GPU case: {case.get('dimension')}/{case.get('operation')}")
            if model == "singleParticle":
                product_dir = ROOT / case["productEvidence"]
                product_worker = product_dir / "worker-result.json"
                project = json.loads((product_dir / "project.json").read_text(encoding="utf-8"))
                product_guard, direct_guard = case["guards"]
            else:
                product_worker = ROOT / case["productWorker"]
                product_dir = product_worker.parent
                project_path = ROOT / case["directReference"]
                project_path = project_path.with_name(project_path.name.replace(".direct.json", ".project.json"))
                project = json.loads(project_path.read_text(encoding="utf-8"))
                product_guard, direct_guard = case["guards"]
            worker = json.loads(product_worker.read_text(encoding="utf-8"))
            step = project["steps"][0]
            actual = next((record for record in worker.get("stepRecords", []) if record.get("stepId") == step["id"]), {})
            if actual.get("actualEngine") != "GPU_DISK" or worker.get("versions") != case.get("versions"):
                raise SystemExit(f"product metadata differs for {model}: {case.get('dimension')}/{case.get('operation')}")
            rows.append(gpu_row(project, step, model, case["dimension"], {
                "productWorker": str(product_worker.relative_to(ROOT)),
                "productGuard": product_guard,
                "directReference": case["directReference"],
                "directGuard": direct_guard,
                "comparisons": case["comparisons"],
                "versions": case["versions"],
                "seed": actual.get("actualSeed"),
                "actualEngine": actual.get("actualEngine"),
            }))


def add_default_ui_multiparticle_row(rows: list[dict]) -> None:
    report = json.loads(DEFAULT_UI_MULTIPARTICLE.read_text(encoding="utf-8"))
    comparisons = report.get("comparisons", {})
    if report.get("status") != "passed" or not comparisons or not all(value is True for value in comparisons.values()):
        raise SystemExit("WebUI default MultiParticle GPU product/direct comparison is incomplete")
    project_path = ROOT / report["project"]
    product_path = ROOT / report["productWorker"]
    project = json.loads(project_path.read_text(encoding="utf-8"))
    worker = json.loads(product_path.read_text(encoding="utf-8"))
    step = project["steps"][0]
    actual = next((record for record in worker.get("stepRecords", []) if record.get("stepId") == step["id"]), {})
    if (report.get("engine") != "GPU_DISK" or report.get("versions") != {"viennaps": "4.6.2", "viennals": "5.8.5"}
            or actual.get("actualEngine") != "GPU_DISK" or actual.get("requestedEnginePreference") != "gpu"):
        raise SystemExit("WebUI default MultiParticle GPU execution metadata does not match")
    rows.append(gpu_row(project, step, "multiParticle", project["dimension"], {
        "productWorker": report["productWorker"],
        "productGuard": report["productGuard"],
        "directReference": report["directReference"],
        "directGuard": report["directGuard"],
        "comparisons": comparisons,
        "versions": report["versions"],
        "seed": actual.get("actualSeed"),
        "cpuThreads": worker.get("cpuThreads"),
        "actualEngine": actual.get("actualEngine"),
    }))


def add_multiparticle_split_flux_rows(rows: list[dict]) -> None:
    """Publish only exact signatures with native process, flux, and browser proofs."""
    summary = json.loads(MULTIPARTICLE_SPLIT_FLUX_SUMMARY.read_text(encoding="utf-8"))
    browser = summary.get("browserAcceptance", {})
    if (summary.get("status") != "passed" or summary.get("versions") != ENGINE_VERSIONS
            or browser.get("status") != "passed" or len(summary.get("cases", [])) != 2):
        raise SystemExit("multi-ion split process/flux/browser evidence is incomplete")
    browser_path = ROOT / browser.get("report", "")
    if not browser_path.is_file():
        raise SystemExit("multi-ion split browser evidence is missing")
    browser_report = json.loads(browser_path.read_text(encoding="utf-8"))
    if (browser_report.get("status") != "passed"
            or browser_report.get("installedVersions") != ENGINE_VERSIONS
            or {row.get("dimension") for row in browser_report.get("cases", [])} != {2, 3}):
        raise SystemExit("multi-ion split Chromium/API acceptance is incomplete")
    for case in summary["cases"]:
        project_path = ROOT / case["project"]
        process_path = ROOT / case["processAcceptance"]
        flux_path = ROOT / case["fluxAcceptance"]
        for path in (project_path, process_path, flux_path):
            if not path.is_file():
                raise SystemExit(f"multi-ion split evidence is missing: {path.relative_to(ROOT)}")
        process = json.loads(process_path.read_text(encoding="utf-8"))
        flux = json.loads(flux_path.read_text(encoding="utf-8"))
        expected = "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d"
        if (process.get("status") != "passed" or flux.get("status") != "passed"
                or process.get("device", {}).get("uuid") != expected
                or flux.get("device", {}).get("uuid") != expected):
            raise SystemExit(f"multi-ion process or flux comparison failed for {case.get('dimension')}D")
        project = json.loads(project_path.read_text(encoding="utf-8"))
        step = next(row for row in project["steps"] if row.get("enabled"))
        evidence = {
            "productWorker": case["processWorker"], "productGuard": case["processGuard"],
            "directReference": case["processDirect"], "directGuard": case["processDirectGuard"],
            "multiIonProcessAcceptance": case["processAcceptance"],
            "singlePassFluxAcceptance": case["fluxAcceptance"],
            "browserReport": browser["report"], "versions": ENGINE_VERSIONS,
            "actualEngine": "GPU_DISK",
        }
        row = gpu_row(project, step, "multiParticle", project["dimension"], evidence)
        row["executionStrategy"] = "symmetric-operator-splitting-v1"
        row["singlePassFluxStrategy"] = "per-species-native-mesh-merge-v1"
        rows.append(row)


def add_multiparticle_split_geometry_rows(rows: list[dict]) -> list[dict]:
    """Publish representative multi-ion process classes backed by direct CPU + browser replay evidence."""
    summary = json.loads(MULTIPARTICLE_SPLIT_GEOMETRY_SUMMARY.read_text(encoding="utf-8"))
    if (summary.get("status") != "passed" or summary.get("versions") != ENGINE_VERSIONS
            or len(summary.get("cases", [])) < 2):
        raise SystemExit("multi-ion geometry-scope direct/browser acceptance is incomplete")
    profile_failures = summary.get("knownGpuProfileFailures", [])
    for failure in profile_failures:
        required = ("dimension", "model", "operation", "boundaryXY", "geometryKind", "solverProfile",
                    "materials", "materialRoles", "materialBindings", "speciesSignature", "reason")
        if (not all(key in failure for key in required) or failure.get("model") != "multiParticle"
                or failure.get("engine", "GPU_DISK") != "GPU_DISK"
                or not failure.get("solverProfile")):
            raise SystemExit(f"malformed known GPU profile failure record: {failure}")
    seen: set[tuple[int, str, str]] = set()
    for case in summary["cases"]:
        project_path = ROOT / case["project"]
        direct_path = ROOT / case["directReference"]
        direct_guard_path = ROOT / case["directGuard"]
        acceptance_path = ROOT / case["browserAcceptance"]
        comparison_path = ROOT / case["processComparison"]
        replay_comparison_path = ROOT / case["replayComparison"]
        paths = (project_path, direct_path, direct_guard_path, acceptance_path,
                 comparison_path, replay_comparison_path)
        if not all(path.is_file() for path in paths):
            raise SystemExit(f"multi-ion geometry evidence file is missing for {case.get('name')}")
        project = json.loads(project_path.read_text(encoding="utf-8"))
        step = next(row for row in project["steps"] if row.get("enabled"))
        acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
        process_comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
        replay_comparison = json.loads(replay_comparison_path.read_text(encoding="utf-8"))
        guard = json.loads(direct_guard_path.read_text(encoding="utf-8"))
        additional_seed_path = ROOT / case["additionalSeedComparison"] if case.get("additionalSeedComparison") else None
        additional_seed = json.loads(additional_seed_path.read_text(encoding="utf-8")) if additional_seed_path else None
        expected_uuid = "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d"
        if (acceptance.get("status") != "passed"
                or acceptance.get("versions") != ENGINE_VERSIONS
                or acceptance.get("selectedGpuDevice", {}).get("uuid") != expected_uuid
                or acceptance.get("dimension") != project["dimension"]
                or acceptance.get("geometry") != project["geometry"]["kind"]
                or acceptance.get("representativeProfileValidationStatus", 200) != 200
                or (project["dimension"] == 3 and project["geometry"]["kind"] == "trench"
                    and acceptance.get("knownFailureProfileValidationStatus", 422) != 422)
                or process_comparison.get("status") != "passed"
                or not all(process_comparison.get("checks", {}).values())
                or replay_comparison.get("status") != "passed"
                or not all(replay_comparison.get("checks", {}).values())
                or guard.get("returnCode") != 0 or guard.get("timedOut")
                or guard.get("rssLimited") or guard.get("remainingOwnedGroupMembers")):
            raise SystemExit(f"multi-ion geometry process/replay evidence failed for {case.get('name')}")
        if additional_seed is not None:
            seed_cases = additional_seed.get("cases", [])
            if (not additional_seed.get("initialMatchesDirect")
                    or not all(additional_seed.get("guards", {}).values())
                    or len(seed_cases) != 1
                    or not seed_cases[0].get("comparisons", {}).get("withinOneTenthGrid")
                    or not seed_cases[0].get("comparisons", {}).get("callbacksObservedForEverySpecies")
                    or any(seed_cases[0].get("comparisons", {}).get(field) is not True for field in (
                        "topologyMatchesDirect", "cellMaterialIdsMatchDirect",
                        "levelSetMaterialMapMatchesDirect", "levelSetCountMatchesDirect"))):
                raise SystemExit(f"additional random-seed comparison failed for {case.get('name')}")
        native_probe_evidence = {}
        if case.get("nativeSplitProbeComparison"):
            native_comparison_path = ROOT / case["nativeSplitProbeComparison"]
            native_output_path = ROOT / case.get("nativeSplitProbeOutput", "")
            native_guard_path = ROOT / case.get("nativeSplitProbeGuard", "")
            extra_output_path = ROOT / case.get("additionalSeedGpuProbe", "")
            extra_guard_path = ROOT / case.get("additionalSeedGpuGuard", "")
            required = (native_comparison_path, native_output_path, native_guard_path,
                        extra_output_path, extra_guard_path)
            if not all(path.is_file() for path in required):
                raise SystemExit(f"native split probe evidence is missing for {case.get('name')}")
            native_comparison = json.loads(native_comparison_path.read_text(encoding="utf-8"))
            native_output = json.loads(native_output_path.read_text(encoding="utf-8"))
            extra_output = json.loads(extra_output_path.read_text(encoding="utf-8"))
            native_guard = json.loads(native_guard_path.read_text(encoding="utf-8"))
            extra_guard = json.loads(extra_guard_path.read_text(encoding="utf-8"))
            direct_cases = native_comparison.get("cases", [])
            expected_uuid = "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d"
            if (not native_comparison.get("initialMatchesDirect")
                    or not all(native_comparison.get("guards", {}).values())
                    or len(direct_cases) != 1
                    or any(direct_cases[0].get("comparisons", {}).get(field) is not True for field in (
                        "topologyMatchesDirect", "cellMaterialIdsMatchDirect", "levelSetMaterialMapMatchesDirect",
                        "levelSetCountMatchesDirect", "withinOneTenthGrid", "callbacksObservedForEverySpecies"))
                    or native_output.get("status") != "succeeded" or native_output.get("engine") != "GPU_DISK"
                    or native_output.get("device", {}).get("uuid") != expected_uuid
                    or native_output.get("execution", {}).get("strategy") != "symmetric-operator-splitting-v1"
                    or native_output.get("execution", {}).get("stageDeviceUuids") != [expected_uuid]
                    or extra_output.get("status") != "succeeded" or extra_output.get("engine") != "GPU_DISK"
                    or extra_output.get("device", {}).get("uuid") != expected_uuid
                    or extra_output.get("execution", {}).get("strategy") != "symmetric-operator-splitting-v1"
                    or extra_output.get("execution", {}).get("stageDeviceUuids") != [expected_uuid]
                    or native_guard.get("returnCode") != 0 or native_guard.get("remainingOwnedGroupMembers")
                    or extra_guard.get("returnCode") != 0 or extra_guard.get("remainingOwnedGroupMembers")):
                raise SystemExit(f"native split probe failed direct structural/GPU checks for {case.get('name')}")
            native_probe_evidence = {
                "nativeSplitProbeComparison": case["nativeSplitProbeComparison"],
                "nativeSplitProbeOutput": case["nativeSplitProbeOutput"],
                "nativeSplitProbeGuard": case["nativeSplitProbeGuard"],
                "additionalSeedGpuProbe": case["additionalSeedGpuProbe"],
                "additionalSeedGpuGuard": case["additionalSeedGpuGuard"],
            }
        signature = gpu_project_signature(project, step)
        profile = signature["solver_profile"]
        # Keep the exercised settings as evidence provenance. Runtime matching
        # uses the representative model/geometry/material class, with known
        # mismatching profiles explicitly listed in knownGpuProfileFailures.
        profile_key = (int(project["dimension"]), str(project["geometry"]["kind"]),
                       json.dumps(profile, sort_keys=True, separators=(",", ":")))
        if profile_key in seen:
            raise SystemExit(f"duplicate multi-ion solver profile in geometry evidence: {case.get('name')}")
        seen.add(profile_key)
        run = gpu_row(project, step, "multiParticle", int(project["dimension"]), {
            "productWorker": acceptance["productWorker"],
            "productGuard": acceptance["productGuard"],
            **({"productDataTransferManifest": case["productDataTransferManifest"]}
               if case.get("productDataTransferManifest") else {}),
            "directReference": str(direct_path.relative_to(ROOT)),
            "directGuard": str(direct_guard_path.relative_to(ROOT)),
            "multiIonProcessAcceptance": str(acceptance_path.relative_to(ROOT)),
            "processComparison": str(comparison_path.relative_to(ROOT)),
            "replayComparison": str(replay_comparison_path.relative_to(ROOT)),
            **({"additionalSeedComparison": str(additional_seed_path.relative_to(ROOT))}
               if additional_seed_path else {}),
            **native_probe_evidence,
            "browserReport": str(acceptance_path.relative_to(ROOT)),
            "versions": ENGINE_VERSIONS,
            "actualEngine": "GPU_DISK",
        }, solver_profiled=True)
        run["executionStrategy"] = "symmetric-operator-splitting-v1"
        rows.append(run)
    return profile_failures


def add_geometry_preset_rows(rows: list[dict]) -> None:
    report = json.loads(GEOMETRY_PRESETS.read_text(encoding="utf-8"))
    if (report.get("status") != "passed" or report.get("passed") is not True
            or report.get("versions") != {"viennaps": "4.6.2", "viennals": "5.8.5"}):
        raise SystemExit("preset geometry GPU_DISK direct-reference matrix is incomplete")
    for case in report.get("rows", []):
        if (case.get("status") != "verified" or case.get("engine") != "GPU_DISK"
                or case.get("productReturnCode") != 0 or case.get("directReturnCode") != 0
                or case.get("productActualEngine") != "GPU_DISK"
                or case.get("directActualEngine") != "GPU_DISK"
                or case.get("productVersions") != report["versions"]
                or case.get("directVersions") != report["versions"]
                or not case.get("comparisons") or not all(value is True for value in case["comparisons"].values())):
            raise SystemExit(f"unverified geometry preset GPU case: {case.get('name')}")
        project_path = ROOT / case["project"]
        product_path = project_path.parent / "worker-result.json"
        direct_path = ROOT / case["directReference"]
        project = json.loads(project_path.read_text(encoding="utf-8"))
        step = project["steps"][0]
        worker = json.loads(product_path.read_text(encoding="utf-8"))
        product_step = next((row for row in worker.get("stepRecords", []) if row.get("stepId") == step["id"]), {})
        if (product_step.get("actualEngine") != "GPU_DISK"
                or worker.get("versions") != report["versions"]
                or not direct_path.is_file()):
            raise SystemExit(f"geometry preset worker/direct artifacts do not match: {case['name']}")
        evidence = {
            "productWorker": str(product_path.relative_to(ROOT)),
            "productGuard": case["productGuard"],
            "directReference": str(direct_path.relative_to(ROOT)),
            "directGuard": case["directGuard"],
            "presetAcceptance": str((project_path.parent / "acceptance.json").relative_to(ROOT)),
            "comparisons": case["comparisons"],
            "versions": report["versions"],
            "seed": product_step.get("actualSeed"),
            "actualEngine": product_step.get("actualEngine"),
        }
        row = gpu_row(project, step, case["model"], case["dimension"], evidence)
        if case.get("finExtrusionMode") is not None:
            row["finExtrusionMode"] = case["finExtrusionMode"]
        rows.append(row)


def add_gds_geometry_rows(rows: list[dict]) -> int:
    """Publish only bounded GPU GDS cases compared with direct ViennaPS."""
    report = json.loads(GDS_GEOMETRY.read_text(encoding="utf-8"))
    material_report = json.loads(GDS_MATERIAL_SCOPE.read_text(encoding="utf-8"))
    multilayer_report = json.loads(GDS_MULTILAYER.read_text(encoding="utf-8"))
    layer_count_report = json.loads(GDS_LAYER_COUNT.read_text(encoding="utf-8"))
    expected_versions = ENGINE_VERSIONS
    if (report.get("passed") is not True or report.get("engine") != "GPU_DISK"
            or report.get("installedVersions") != expected_versions
            or report.get("caseCount") != 24 or report.get("expectedCaseCount") != 24):
        raise SystemExit("GDS GPU direct-reference matrix is incomplete")
    if (material_report.get("passed") is not True
            or material_report.get("engine") != "GPU_DISK"
            or material_report.get("installedVersions") != expected_versions
            or material_report.get("caseCount") != 300
            or material_report.get("expectedCaseCount") != 300):
        raise SystemExit("GDS GPU native material-scope matrix is incomplete")
    if (multilayer_report.get("passed") is not True
            or multilayer_report.get("engine") != "GPU_DISK"
            or multilayer_report.get("installedVersions") != expected_versions
            or multilayer_report.get("caseCount") != 80
            or multilayer_report.get("expectedCaseCount") != 80):
        raise SystemExit("GDS GPU multi-layer direct-reference matrix is incomplete")
    if (layer_count_report.get("passed") is not True
            or layer_count_report.get("engine") != "GPU_DISK"
            or layer_count_report.get("installedVersions") != expected_versions
            or layer_count_report.get("mappingCount") != 64
            or layer_count_report.get("caseCount") != 32
            or layer_count_report.get("expectedCaseCount") != 32):
        raise SystemExit("GDS GPU maximum 64-layer direct-reference matrix is incomplete")

    # Re-derive the advertised GDS scope from the completed direct-reference
    # sweep. The UI and backend may vary material identities only within these
    # exact native catalog IDs/roles and single-layer operation bindings.
    builtin_catalog = json.loads((ROOT / "docs/materials-catalog.json").read_text(encoding="utf-8"))
    builtin_ids = sorted(int(item["id"]) for item in builtin_catalog["materials"])
    expected_builtins = {item["name"] for item in builtin_catalog["materials"]}
    material_rows = material_report.get("rows", [])
    if len(material_rows) != 300:
        raise SystemExit("GDS GPU material-scope rows are missing")
    seen_film: set[tuple[str, str]] = set()
    seen_mask: set[tuple[str, str]] = set()
    seen_deposits: set[tuple[str, str]] = set()
    seen_custom_roles: set[tuple[str, int, str]] = set()
    seen_custom_deposits: set[tuple[str, int]] = set()
    custom_ids: set[int] = set()
    for case in material_rows:
        comparisons = case.get("comparisons", {})
        if (case.get("status") != "verified" or case.get("engine") != "GPU_DISK"
                or case.get("productReturnCode") != 0 or case.get("directReturnCode") != 0
                or case.get("productActualEngine") != "GPU_DISK"
                or case.get("directActualEngine") != "GPU_DISK"
                or case.get("productVersions") != expected_versions
                or case.get("directVersions") != expected_versions
                or not comparisons or not all(value is True for value in comparisons.values())):
            raise SystemExit(f"unverified GDS material GPU case: {case.get('name')}")
        for field in ("productGuard", "directGuard"):
            guard_path = ROOT / case[field]
            if not guard_path.is_file():
                raise SystemExit(f"GDS material bounded-child evidence is missing: {case.get(field)}")
            guard = json.loads(guard_path.read_text(encoding="utf-8"))
            if (guard.get("returnCode") != 0 or guard.get("timedOut") or guard.get("rssLimited")
                    or guard.get("remainingOwnedGroupMembers")):
                raise SystemExit(f"GDS material bounded child did not exit cleanly: {case.get(field)}")
        project = json.loads((ROOT / case["project"]).read_text(encoding="utf-8"))
        if project.get("geometry", {}).get("kind") != "gds":
            raise SystemExit(f"GDS material scope contains non-GDS project: {case.get('name')}")
        boundary = case.get("boundaryXY")
        if boundary not in {"reflective", "periodic"}:
            raise SystemExit(f"GDS material scope has unsupported boundary: {case.get('name')}")
        step = project["steps"][0]
        dimension = int(project["dimension"])
        layer = project["geometry"]["layers"][0]
        material = layer["material"]
        is_custom = bool(project.get("materialDefinitions"))
        if is_custom:
            # Projects carry the complete ordered 64-ID registry so IDs remain
            # stable across processes. Only the edge identity used by this
            # particular geometry/binding is counted as directly exercised.
            custom_ids.update(int(value) for value in case.get("customMaterialIds", []))
        elif material not in expected_builtins:
            raise SystemExit(f"GDS material is outside the installed catalog: {material}")
        if step["operation"] == "deposit":
            deposit_material = step.get("depositMaterial")
            if deposit_material not in expected_builtins and not is_custom:
                raise SystemExit(f"GDS deposit material is outside the installed catalog: {case.get('name')}")
            if not is_custom:
                seen_deposits.add((boundary, deposit_material))
            else:
                seen_custom_deposits.update((boundary, int(item["id"]))
                                            for item in project["materialDefinitions"]
                                            if item["name"] == deposit_material)
        elif layer.get("mask", False):
            if dimension != 3:
                raise SystemExit(f"GDS material scope uses a 2D mask mapping: {case.get('name')}")
            if not is_custom:
                seen_mask.add((boundary, material))
            else:
                seen_custom_roles.update((boundary, int(item["id"]), "mask")
                                         for item in project["materialDefinitions"]
                                         if item["name"] == material)
        elif not is_custom:
            seen_film.add((boundary, material))
        else:
            seen_custom_roles.update((boundary, int(item["id"]), "film")
                                     for item in project["materialDefinitions"]
                                     if item["name"] == material)
    required_material_pairs = {(boundary, name) for boundary in ("reflective", "periodic")
                               for name in expected_builtins}
    expected_custom_edges = {(boundary, native_id) for boundary in ("reflective", "periodic")
                             for native_id in (177, 240)}
    if (seen_film != required_material_pairs or seen_mask != required_material_pairs
            or seen_deposits != required_material_pairs or custom_ids != {177, 240}
            or seen_custom_roles != {(boundary, native_id, role)
                                     for boundary, native_id in expected_custom_edges
                                     for role in ("film", "mask")}
            or seen_custom_deposits != expected_custom_edges):
        raise SystemExit("GDS built-in film/mask/deposit or custom native ID coverage is incomplete")

    expected_profile_keys = {
        (2, ("film",), "builtin-distinct"),
        (2, ("film",), "builtin-repeated"),
        (2, ("film",), "custom-edge-pair"),
        *{(3, (role,), material_class)
          for role in ("film", "mask")
          for material_class in ("builtin-distinct", "builtin-repeated")},
        (3, ("film", "mask"), "builtin-distinct"),
        (3, ("film", "mask"), "builtin-repeated"),
        (3, ("film", "mask"), "custom-edge-pair"),
    }
    expected_cross_product = {(boundary, model, operation)
                              for boundary in ("reflective", "periodic")
                              for model in ("singleParticle", "multiParticle")
                              for operation in ("etch", "deposit")}
    multilayer_coverage: dict[tuple[int, tuple[str, ...], str], set[tuple[str, str, str]]] = {}
    for case in multilayer_report.get("rows", []):
        comparisons = case.get("comparisons", {})
        if (case.get("status") != "verified" or case.get("engine") != "GPU_DISK"
                or case.get("productReturnCode") != 0 or case.get("directReturnCode") != 0
                or case.get("productActualEngine") != "GPU_DISK"
                or case.get("directActualEngine") != "GPU_DISK"
                or case.get("productVersions") != expected_versions
                or case.get("directVersions") != expected_versions
                or not comparisons or not all(value is True for value in comparisons.values())):
            raise SystemExit(f"unverified GDS multi-layer GPU case: {case.get('name')}")
        for field in ("productGuard", "directGuard"):
            guard_path = ROOT / case[field]
            if not guard_path.is_file():
                raise SystemExit(f"GDS multi-layer bounded-child evidence is missing: {case.get(field)}")
            guard = json.loads(guard_path.read_text(encoding="utf-8"))
            if (guard.get("returnCode") != 0 or guard.get("timedOut") or guard.get("rssLimited")
                    or guard.get("remainingOwnedGroupMembers")):
                raise SystemExit(f"GDS multi-layer bounded child did not exit cleanly: {case.get(field)}")
        project = json.loads((ROOT / case["project"]).read_text(encoding="utf-8"))
        if project.get("geometry", {}).get("kind") != "gds" or len(project["geometry"].get("layers", [])) != 2:
            raise SystemExit(f"GDS multi-layer case does not contain exactly two mappings: {case.get('name')}")
        dimension = int(project["dimension"])
        step = project["steps"][0]
        layer_rows = project["geometry"]["layers"]
        roles = tuple(sorted({"mask" if layer.get("mask", False) else "film" for layer in layer_rows}))
        builtin_by_name = {item["name"]: int(item["id"]) for item in builtin_catalog["materials"]}
        definitions_by_name = {item["name"]: int(item["id"])
                               for item in project.get("materialDefinitions", [])}
        ids = [definitions_by_name.get(layer["material"], builtin_by_name.get(layer["material"]))
               for layer in layer_rows]
        if any(value is None for value in ids):
            raise SystemExit(f"GDS multi-layer case has an unknown native material: {case.get('name')}")
        if project.get("materialDefinitions"):
            material_class = "custom-edge-pair"
            if (set(ids) != {177, 240}
                    or sorted(int(value) for value in case.get("customMaterialIds", [])) != [177, 240]):
                raise SystemExit(f"GDS custom multi-layer case does not exercise both registry edges: {case.get('name')}")
        else:
            if not set(ids).issubset(set(builtin_ids)):
                raise SystemExit(f"GDS multi-layer case uses an unmeasured built-in ID: {case.get('name')}")
            material_class = "builtin-distinct" if len(set(ids)) == 2 else "builtin-repeated"
        profile_key = (dimension, roles, material_class)
        if profile_key not in expected_profile_keys:
            raise SystemExit(f"unexpected GDS multi-layer role/material profile: {profile_key}")
        multilayer_coverage.setdefault(profile_key, set()).add(
            (case["boundaryXY"], case["model"], case["operation"]))
    if (set(multilayer_coverage) != expected_profile_keys
            or any(multilayer_coverage[key] != expected_cross_product for key in expected_profile_keys)):
        raise SystemExit("GDS multi-layer GPU role, material identity, operation, model or boundary coverage is incomplete")

    max_layer_coverage: dict[tuple[int, tuple[str, ...]], set[tuple[str, str, str]]] = {}
    for case in layer_count_report.get("rows", []):
        comparisons = case.get("comparisons", {})
        if (case.get("status") != "verified" or case.get("engine") != "GPU_DISK"
                or case.get("productReturnCode") != 0 or case.get("directReturnCode") != 0
                or case.get("productActualEngine") != "GPU_DISK"
                or case.get("directActualEngine") != "GPU_DISK"
                or case.get("productVersions") != expected_versions
                or case.get("directVersions") != expected_versions
                or not comparisons or not all(value is True for value in comparisons.values())):
            raise SystemExit(f"unverified GDS 64-layer GPU case: {case.get('name')}")
        for field in ("productGuard", "directGuard"):
            guard_path = ROOT / case[field]
            if not guard_path.is_file():
                raise SystemExit(f"GDS 64-layer bounded-child evidence is missing: {case.get(field)}")
            guard = json.loads(guard_path.read_text(encoding="utf-8"))
            if (guard.get("returnCode") != 0 or guard.get("timedOut") or guard.get("rssLimited")
                    or guard.get("remainingOwnedGroupMembers")):
                raise SystemExit(f"GDS 64-layer bounded child did not exit cleanly: {case.get(field)}")
        project = json.loads((ROOT / case["project"]).read_text(encoding="utf-8"))
        geometry = project.get("geometry", {})
        if (geometry.get("kind") != "gds" or len(geometry.get("layers", [])) != 64
                or float(project["domain"]["gridDelta"]) != 0.1
                or geometry.get("position") != [0.0, 0.0, 0.0]):
            raise SystemExit(f"GDS maximum mapping case is not the grid-aligned 64-layer project: {case.get('name')}")
        roles = tuple(sorted({"mask" if layer.get("mask", False) else "film"
                              for layer in geometry["layers"]}))
        profile = (int(project["dimension"]), roles)
        if profile not in {(2, ("film",)), (3, ("film",)), (3, ("mask",)), (3, ("film", "mask"))}:
            raise SystemExit(f"unexpected GDS 64-layer role profile: {profile}")
        max_layer_coverage.setdefault(profile, set()).add(
            (case["boundaryXY"], case["model"], case["operation"]))
    expected_layer_profiles = {(2, ("film",)), (3, ("film",)), (3, ("mask",)), (3, ("film", "mask"))}
    if (set(max_layer_coverage) != expected_layer_profiles
            or any(max_layer_coverage[key] != expected_cross_product for key in expected_layer_profiles)):
        raise SystemExit("GDS 64-layer GPU dimension, role, operation, model or boundary coverage is incomplete")

    for case in report["rows"]:
        comparisons = case.get("comparisons", {})
        if (case.get("status") != "verified" or case.get("engine") != "GPU_DISK"
                or case.get("productReturnCode") != 0 or case.get("directReturnCode") != 0
                or case.get("productActualEngine") != "GPU_DISK"
                or case.get("directActualEngine") != "GPU_DISK"
                or case.get("productVersions") != expected_versions
                or case.get("directVersions") != expected_versions
                or not comparisons or not all(value is True for value in comparisons.values())):
            raise SystemExit(f"unverified GDS GPU case: {case.get('name')}")
        for field in ("productGuard", "directGuard"):
            guard_path = ROOT / case[field]
            if not guard_path.is_file():
                raise SystemExit(f"GDS bounded-child evidence is missing: {case.get(field)}")
            guard = json.loads(guard_path.read_text(encoding="utf-8"))
            if (guard.get("returnCode") != 0 or guard.get("timedOut") or guard.get("rssLimited")
                    or guard.get("remainingOwnedGroupMembers")):
                raise SystemExit(f"GDS bounded child did not exit cleanly: {case.get(field)}")
        project_path = ROOT / case["project"]
        direct_path = ROOT / case["directReference"]
        project = json.loads(project_path.read_text(encoding="utf-8"))
        step = project["steps"][0]
        worker_path = project_path.parent / "worker-result.json"
        if not worker_path.is_file() or not direct_path.is_file():
            raise SystemExit(f"GDS GPU artifact is missing: {case.get('name')}")
        worker = json.loads(worker_path.read_text(encoding="utf-8"))
        product_step = next((item for item in worker.get("stepRecords", [])
                             if item.get("stepId") == step["id"]), {})
        if worker.get("versions") != expected_versions or product_step.get("actualEngine") != "GPU_DISK":
            raise SystemExit(f"GDS product GPU metadata differs: {case.get('name')}")
        row = gpu_row(project, step, case["model"], case["dimension"], {
            "productWorker": str(worker_path.relative_to(ROOT)),
            "productGuard": case["productGuard"],
            "directReference": str(direct_path.relative_to(ROOT)),
            "directGuard": case["directGuard"],
            "geometryAcceptance": str((project_path.parent / "acceptance.json").relative_to(ROOT)),
            "gdsAssetId": project["geometry"]["assetId"],
            "gdsStructure": project["geometry"]["structure"],
            "gdsLayers": project["geometry"]["layers"],
            "comparisons": comparisons, "versions": expected_versions,
            "seed": product_step.get("actualSeed"), "actualEngine": product_step.get("actualEngine"),
        })
        row["gdsScope"] = {
            "kind": "viennaps-gds-process-v1", "engineVersion": "4.6.2",
            "dimensions": [2, 3], "maxLayerMappings": 64, "supports3DMask": True,
            "evidence": [str(GDS_GEOMETRY.relative_to(ROOT)),
                         str(GDS_MULTILAYER.relative_to(ROOT)),
                         str(GDS_LAYER_COUNT.relative_to(ROOT))],
        }
        row["materialScope"] = {
            "kind": "viennaps-native-material-registry-v1",
            "engineVersion": "4.6.2",
            "builtInLegacyIds": builtin_ids,
            "customLegacyIdRange": [177, 240],
            "boundaries": [row["boundaryXY"]],
            "roleKindsByGeometry": {"gds": ([ ["film"] ] if row["dimension"] == 2 else
                                              [["film"], ["mask"], ["film", "mask"]])},
            "bindingKeysByOperation": {"etch": [], "deposit": ["depositMaterial"]},
            "evidence": [str(GDS_GEOMETRY.relative_to(ROOT)),
                         str(GDS_MATERIAL_SCOPE.relative_to(ROOT)),
                         str(GDS_MULTILAYER.relative_to(ROOT)),
                         str(GDS_LAYER_COUNT.relative_to(ROOT))],
        }
        if row.get("model") == "multiParticle":
            row["speciesScope"] = {"kind": "material-tables-v1",
                                    "evidence": [str(GDS_MATERIAL_SCOPE.relative_to(ROOT)),
                                                 str(GDS_MULTILAYER.relative_to(ROOT)),
                                                 str(GDS_LAYER_COUNT.relative_to(ROOT))]}
        rows.append(row)
    return report["caseCount"]


def add_specialized_gds_rows(rows: list[dict]) -> list[dict]:
    """Add directly matched specialized/GDS signatures after the periodic extent repair."""
    report = json.loads(GDS_SPECIALIZED.read_text(encoding="utf-8"))
    if (report.get("engine") != "GPU_DISK" or report.get("installedVersions") != ENGINE_VERSIONS
            or report.get("caseCount") != 40 or report.get("expectedCaseCount") != 40):
        raise SystemExit("specialized GDS GPU report is incomplete")
    browser_report = json.loads(GDS_SPECIALIZED_BROWSER.read_text(encoding="utf-8"))
    browser_case = next((case for case in browser_report.get("cases", [])
                         if case.get("name") == "3d-teos-gds-distinct-deposition-and-mask-materials"), None)
    if (browser_report.get("status") != "passed" or not browser_case
            or browser_case.get("actualEngine") != "GPU_DISK"
            or not browser_case.get("initialAndFinalExactlyMatchDirect")):
        raise SystemExit("specialized GDS Chromium TEOS GPU acceptance is incomplete")
    if GDS_PERIODIC_BROWSER.is_file():
        periodic_browser = json.loads(GDS_PERIODIC_BROWSER.read_text(encoding="utf-8"))
        periodic_case = periodic_browser.get("periodicGdsGpu", {})
        if (periodic_browser.get("status") != "passed"
                or periodic_case.get("actualEngine") != "GPU_DISK"
                or not periodic_case.get("initialAndFinalExactlyMatchDirect")
                or periodic_case.get("periodicBoundsMatch") is not True
                or periodic_case.get("runStatus") != "succeeded"):
            raise SystemExit("specialized periodic GDS Chromium GPU acceptance is incomplete")
    accepted: set[tuple[str, int, str]] = set()
    for case in report["rows"]:
        key = (case.get("model"), case.get("dimension"), case.get("boundaryXY"))
        comparisons = case.get("comparisons", {})
        if (case.get("status") != "verified" or case.get("productReturnCode") != 0
                or case.get("directReturnCode") != 0 or case.get("productActualEngine") != "GPU_DISK"
                or case.get("directActualEngine") != "GPU_DISK"
                or case.get("productVersions") != ENGINE_VERSIONS
                or case.get("directVersions") != ENGINE_VERSIONS
                or not comparisons or not all(value is True for value in comparisons.values())):
            raise SystemExit(f"specialized GDS product/direct comparison differs: {case.get('name')}")
        if case.get("boundaryXY") == "periodic" and not all(
                comparisons.get(name) is True for name in
                ("periodicSupportLayer", "periodicSupportBounds", "periodicNativeBounds")):
            raise SystemExit(f"specialized periodic GDS extent support differs: {case.get('name')}")
        project_path = ROOT / case["project"]
        direct_path = ROOT / case["directReference"]
        worker_path = project_path.parent / "worker-result.json"
        if not all(path.is_file() for path in (project_path, direct_path, worker_path)):
            raise SystemExit(f"specialized GDS evidence is missing: {case.get('name')}")
        project = json.loads(project_path.read_text(encoding="utf-8"))
        worker = json.loads(worker_path.read_text(encoding="utf-8"))
        step = project["steps"][0]
        actual = next((item for item in worker.get("stepRecords", [])
                       if item.get("stepId") == step["id"]), {})
        native = actual.get("actualParameters", {}).get("nativeModel", {})
        if (worker.get("versions") != ENGINE_VERSIONS or actual.get("actualEngine") != "GPU_DISK"
                or native.get("nativeModel") != case.get("model")):
            raise SystemExit(f"specialized GDS native readback differs: {case.get('name')}")
        evidence = {
            "productWorker": str(worker_path.relative_to(ROOT)),
            "productGuard": case["productGuard"],
            "directReference": case["directReference"],
            "directGuard": case["directGuard"],
            "geometryAcceptance": str((project_path.parent / "acceptance.json").relative_to(ROOT)),
            "comparisons": comparisons, "versions": ENGINE_VERSIONS,
            "seed": actual.get("actualSeed"), "actualEngine": actual.get("actualEngine"),
            "gdsLayerMappings": case.get("gdsLayerMappings"),
        }
        if (case.get("model") == "teosDeposition" and case.get("dimension") == 3
                and case.get("boundaryXY") == "reflective"):
            evidence["browserReport"] = str(GDS_SPECIALIZED_BROWSER.relative_to(ROOT))
        if case.get("boundaryXY") == "periodic":
            evidence["periodicBrowserReport"] = str(GDS_PERIODIC_BROWSER.relative_to(ROOT))
        row = gpu_row(project, step, f"specialized:{case['model']}", case["dimension"], evidence)
        row["gdsScope"] = {
            "kind": "viennaps-gds-process-v1", "engineVersion": "4.6.2",
            "dimensions": [case["dimension"]], "maxLayerMappings": 2,
            "supports3DMask": case["dimension"] == 3,
            "evidence": [str(GDS_SPECIALIZED.relative_to(ROOT))],
        }
        rows.append(row)
        accepted.add(key)
        for field in ("productGuard", "directGuard"):
            guard_path = ROOT / case[field]
            if not guard_path.is_file():
                raise SystemExit(f"specialized GDS bounded-child evidence is missing: {case.get(field)}")
            guard = json.loads(guard_path.read_text(encoding="utf-8"))
            if (guard.get("returnCode") != 0 or guard.get("timedOut") or guard.get("rssLimited")
                    or guard.get("remainingOwnedGroupMembers")):
                raise SystemExit(f"specialized GDS child was not bounded/clean: {case.get(field)}")
    expected = {(model, dimension, boundary) for model in sorted(
        spec["id"] for spec in specialized_catalog()["models"] if spec["usesFlux"])
        for dimension in (2, 3) for boundary in ("reflective", "periodic")}
    if accepted != expected:
        raise SystemExit(f"specialized GDS coverage differs: missing={sorted(expected - accepted)}")
    for required in (GDS_SPECIALIZED_SINGLE, GDS_SPECIALIZED_OFFSET, GDS_SPECIALIZED_MASK,
                    GDS_SPECIALIZED_CPU_MATRIX, GDS_SPECIALIZED_BROWSER):
        if not required.is_file():
            raise SystemExit(f"specialized GDS diagnostic evidence is missing: {required.relative_to(ROOT)}")
    cpu_matrix = json.loads(GDS_SPECIALIZED_CPU_MATRIX.read_text(encoding="utf-8"))
    if (cpu_matrix.get("engine") != "CPU_DISK" or cpu_matrix.get("caseCount") != 40
            or cpu_matrix.get("expectedCaseCount") != 40 or not cpu_matrix.get("passed")
            or not all(row.get("status") == "verified" for row in cpu_matrix.get("rows", []))):
        raise SystemExit("CPU_DISK specialized GDS product/direct matrix is incomplete")
    for report_path in (GDS_SPECIALIZED_SINGLE, GDS_SPECIALIZED_OFFSET, GDS_SPECIALIZED_MASK):
        profile = json.loads(report_path.read_text(encoding="utf-8"))
        if (profile.get("engine") != "GPU_DISK" or profile.get("caseCount") != 8
                or profile.get("expectedCaseCount") != 8 or not profile.get("passed")
                or not all(row.get("status") == "verified" for row in profile.get("rows", []))):
            raise SystemExit(f"specialized periodic GDS profile report is incomplete: {report_path.name}")
    return []


def verify_patched_gpu_multiion() -> dict:
    """Ensure published capability metadata reflects the repaired runtime."""
    report = json.loads(MULTIPARTICLE_PATCHED_EIGHT_ION.read_text(encoding="utf-8"))
    expected_labels = [f"ion{index}" for index in range(8)]
    expected_uuid = "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d"
    if (report.get("status") != "passed" or report.get("deviceUuid") != expected_uuid
            or len(report.get("cases", [])) != 4):
        raise SystemExit("patched GPU MultiParticle eight-ion CPU/GPU direct evidence is incomplete")
    runtime_root = Path(report["runtimeDir"]).resolve(strict=True)
    runtime_fingerprint = fingerprint_package(runtime_root / "viennaps")
    dimensions = {2, 3}
    for dimension in dimensions:
        cases = [row for row in report["cases"] if row.get("dimension") == dimension]
        if {row.get("engine") for row in cases} != {"CPU_DISK", "GPU_DISK"}:
            raise SystemExit(f"patched GPU MultiParticle direct engine pair is incomplete: {dimension}D")
        for row in cases:
            if row.get("returnCode") != 0 or row.get("speciesOrder") != expected_labels:
                raise SystemExit(f"patched MultiParticle species evidence is incomplete: {dimension}D/{row.get('engine')}")
            data_path = ROOT / row["output"]
            data = json.loads(data_path.read_text(encoding="utf-8"))
            callback = data.get("callback", {})
            samples = callback.get("samples", [])
            if (data.get("status") != "succeeded" or data.get("actualEngine") != row["engine"]
                    or data.get("versions") != ENGINE_VERSIONS
                    or not Path(data["viennapsModulePath"]).resolve().is_relative_to(runtime_root)
                    or data.get("actualParameters", {}).get("speciesOrder") != expected_labels
                    or callback.get("count", 0) <= 0 or not samples
                    or any(len(sample.get("fluxes", [])) != 8 for sample in samples)
                    or any(not all(math.isfinite(float(value)) for value in sample["fluxes"])
                           for sample in samples)):
                raise SystemExit(f"patched MultiParticle result omitted or corrupted a species: {dimension}D/{row.get('engine')}")
            guard = json.loads((ROOT / row["guard"]).read_text(encoding="utf-8"))
            if (guard.get("returnCode") != 0 or guard.get("timedOut") or guard.get("rssLimited")
                    or guard.get("remainingOwnedGroupMembers")):
                raise SystemExit(f"patched MultiParticle direct child was not clean: {row['guard']}")

    for dimension, path in ((2, MULTIPARTICLE_PATCHED_UI_2D), (3, MULTIPARTICLE_PATCHED_UI_3D)):
        ui = json.loads(path.read_text(encoding="utf-8"))
        if (ui.get("status") != "passed" or ui.get("engine") != "GPU_DISK"
                or ui.get("gpuUuid") != expected_uuid or ui.get("speciesOrder") != expected_labels
                or ui.get("runtimeFingerprint") != runtime_fingerprint
                or not ui.get("comparisons")
                or not all(all(checks.values()) for checks in ui["comparisons"].values())):
            raise SystemExit(f"patched MultiParticle WebUI/direct result comparison is incomplete: {dimension}D")
    for dimension, path in ((2, MULTIPARTICLE_PATCHED_BROWSER_2D),
                            (3, MULTIPARTICLE_PATCHED_BROWSER_3D)):
        browser = json.loads(path.read_text(encoding="utf-8"))
        if (browser.get("status") != "passed" or browser.get("dimension") != dimension
                or browser.get("speciesCount") != 8 or browser.get("gpuUuid") != expected_uuid
                or browser.get("pageErrors")):
            raise SystemExit(f"patched MultiParticle eight-ion browser flow is incomplete: {dimension}D")
    gds = json.loads(MULTIPARTICLE_PATCHED_GDS.read_text(encoding="utf-8"))
    if (gds.get("engine") != "GPU_DISK" or gds.get("installedVersions") != ENGINE_VERSIONS
            or gds.get("passed") is not True or gds.get("caseCount") != 12
            or gds.get("expectedCaseCount") != 12 or len(gds.get("rows", [])) != 12):
        raise SystemExit("patched GDS MultiParticle GPU matrix is incomplete")
    required_gds_checks = {
        "initialPoints", "initialCells", "initialMaterialIds", "initialMaterialMap",
        "finalPoints", "finalCells", "finalMaterialIds", "finalMaterialMap",
        "rateCallbackCount", "rateCallbackMaterials", "rateCallbackFluxSums",
        "rateCallbackFluxMaxima", "rateCallbackMeanFlux", "rateCallbackFormulaSamples",
    }
    for row in gds["rows"]:
        if (row.get("status") != "verified" or row.get("engine") != "GPU_DISK"
                or row.get("productActualEngine") != "GPU_DISK"
                or row.get("directActualEngine") != "GPU_DISK"
                or not required_gds_checks.issubset(row.get("comparisons", {}))
                or not all(row["comparisons"][name] for name in required_gds_checks)):
            raise SystemExit(f"patched GDS MultiParticle GPU comparison failed: {row.get('name')}")
        project_path = ROOT / row["project"]
        project = json.loads(project_path.read_text(encoding="utf-8"))
        worker_path = project_path.parent / "worker-result.json"
        worker = json.loads(worker_path.read_text(encoding="utf-8"))
        product_step = worker["stepRecords"][0]
        direct_path = ROOT / row["directReference"]
        direct = json.loads(direct_path.read_text(encoding="utf-8"))
        labels = [species["label"] for species in project["steps"][0]["species"]]
        product_callback = product_step["actualParameters"]["rateCallbackSummary"]
        direct_callback = direct["callback"]
        if (worker.get("nativeEngineFingerprint") != runtime_fingerprint
                or product_step.get("actualEngine") != "GPU_DISK"
                or not Path(direct["viennapsModulePath"]).resolve().is_relative_to(runtime_root / "viennaps")
                or product_callback.get("invocationCount") != direct_callback.get("count")
                or product_callback.get("fluxSums") != direct_callback.get("fluxSums")
                or product_callback.get("fluxMaxima") != direct_callback.get("fluxMaxima")
                or product_callback.get("meanFluxBySpecies") != direct_callback.get("meanFluxBySpecies")
                or len(direct_callback.get("fluxSums", [])) != len(labels)
                or not all(math.isfinite(float(value)) and float(value) > 0
                           for value in direct_callback["fluxSums"])):
            raise SystemExit(f"patched GDS MultiParticle callback channels are incomplete: {row.get('name')}")
        for key in ("productGuard", "directGuard"):
            guard = json.loads((ROOT / row[key]).read_text(encoding="utf-8"))
            if (guard.get("returnCode") != 0 or guard.get("timedOut") or guard.get("rssLimited")
                    or guard.get("remainingOwnedGroupMembers")):
                raise SystemExit(f"patched GDS MultiParticle child was not clean: {row[key]}")
    profile_report = json.loads(MULTIPARTICLE_PATCHED_PROFILE_RECHECK.read_text(encoding="utf-8"))
    expected_profiles = {
        (2, 0.25, 64), (2, 0.25, 512), (3, 0.25, 64),
        (3, 0.25, 256), (3, 0.20, 1024), (3, 0.25, 1024),
    }
    required_profile_checks = {
        "initialPoints", "initialCells", "initialMaterialIds", "initialMaterialMap",
        "finalPoints", "finalCells", "finalMaterialIds", "finalMaterialMap",
        "productEngine", "directEngine", "versions", "multiParticleSpeciesConfiguration",
        "rateCallbackCount", "rateCallbackMaterials", "rateCallbackFluxSums",
        "rateCallbackFluxMaxima", "rateCallbackMeanFlux", "rateCallbackFormulaSamples",
    }
    if (profile_report.get("verified") != len(expected_profiles) or profile_report.get("failed") != 0
            or len(profile_report.get("cases", [])) != len(expected_profiles)):
        raise SystemExit("patched native multi-ion GPU solver-profile recheck is incomplete")
    verified_profiles = set()
    for profile_case in profile_report["cases"]:
        acceptance_path = ROOT / profile_case["acceptancePath"]
        acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
        project_path = ROOT / acceptance["project"]
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project_solver = project["solver"]
        profile_key = (int(project["dimension"]), float(project_solver["timeStepRatio"]),
                       int(project_solver["raysPerPoint"]))
        verified_profiles.add(profile_key)
        worker_path = project_path.parent / "worker-result.json"
        direct_path = ROOT / acceptance["directReference"]
        worker = json.loads(worker_path.read_text(encoding="utf-8"))
        direct = json.loads(direct_path.read_text(encoding="utf-8"))
        product_step = worker["stepRecords"][0]
        product_params = product_step["actualParameters"]
        callback = profile_case.get("productCallbacks", {})
        if (profile_case.get("status") != "verified" or acceptance.get("status") != "verified"
                or profile_key not in expected_profiles
                or not required_profile_checks.issubset(acceptance.get("comparisons", {}))
                or not all(acceptance["comparisons"][name] is True for name in required_profile_checks)
                or acceptance.get("productActualEngine") != "GPU_DISK"
                or acceptance.get("directActualEngine") != "GPU_DISK"
                or worker.get("nativeEngineFingerprint") != runtime_fingerprint
                or product_params.get("multiParticleExecution", {}).get("strategy") != "native-multi-ion-v1"
                or product_step.get("actualEngine") != "GPU_DISK"
                or direct.get("actualEngine") != "GPU_DISK"
                or direct.get("versions") != ENGINE_VERSIONS
                or not Path(direct["viennapsModulePath"]).resolve().is_relative_to(runtime_root / "viennaps")
                or callback.get("invocationCount", 0) <= 0
                or len(callback.get("fluxSums", [])) != 3
                or any(not math.isfinite(float(value)) or float(value) <= 0 for value in callback["fluxSums"])):
            raise SystemExit(f"patched native GPU profile product/direct comparison failed: {profile_case.get('case')}")
        for guard_key in ("productGuard", "directGuard"):
            guard = json.loads((ROOT / acceptance[guard_key]).read_text(encoding="utf-8"))
            if (guard.get("returnCode") != 0 or guard.get("timedOut") or guard.get("rssLimited")
                    or guard.get("remainingOwnedGroupMembers")):
                raise SystemExit(f"patched native GPU profile child was not clean: {acceptance[guard_key]}")
    if verified_profiles != expected_profiles:
        raise SystemExit(f"patched native GPU profile cases differ: {verified_profiles}")
    profile_browser = json.loads(MULTIPARTICLE_PATCHED_PROFILE_BROWSER.read_text(encoding="utf-8"))
    if (profile_browser.get("status") != "passed" or profile_browser.get("dimension") != 2
            or profile_browser.get("speciesCount") != 3 or profile_browser.get("profileRecheck") is not True
            or profile_browser.get("gpuUuid") != expected_uuid
            or profile_browser.get("processStrategy") != "native-multi-ion-v1"
            or profile_browser.get("timeStepRatio") != 0.25 or profile_browser.get("raysPerPoint") != 64
            or profile_browser.get("pageErrors")):
        raise SystemExit("patched native GPU solver-profile browser acceptance is incomplete")
    cpu_gpu_profile = json.loads(MULTIPARTICLE_PATCHED_CPU_GPU_PROFILE.read_text(encoding="utf-8"))
    cpu_failures = [row for row in cpu_gpu_profile.get("cpuRuns", []) if row.get("status") != "verified"]
    cpu_failure_rerun = cpu_gpu_profile.get("cpuFailureWorkerRerun", {})
    if (cpu_gpu_profile.get("status") != "partial" or cpu_gpu_profile.get("caseCount") != 5
            or cpu_gpu_profile.get("expectedCaseCount") != len(expected_profiles)
            or cpu_gpu_profile.get("failedCpuCaseCount") != 1 or len(cpu_failures) != 1
            or cpu_failures[0].get("case") != "3d-trench-cfl0.2-rays1024"
            or "non-finite species flux" not in cpu_failures[0].get("failureExcerpts", {}).get("direct", "")
            or len(cpu_gpu_profile.get("comparisons", [])) != 5
            or cpu_failure_rerun.get("status") != "reproduced"
            or cpu_failure_rerun.get("returnCode") != 1 or cpu_failure_rerun.get("timedOut")
            or cpu_failure_rerun.get("rssLimited")
            or cpu_failure_rerun.get("remainingOwnedGroupMembers")
            or "non-finite species flux" not in cpu_failure_rerun.get("runtimeError", "")):
        raise SystemExit("same-profile CPU/GPU numerical diagnostic is incomplete or changed unexpectedly")
    relative_flux_differences: dict[str, list[float]] = {"2d": [], "3d": []}
    maximum_vertex_distance = 0.0
    for case in cpu_gpu_profile["comparisons"]:
        dimension = int(case["dimension"])
        if case.get("cpuActualEngine") != "CPU_DISK" or case.get("gpuActualEngine") != "GPU_DISK":
            raise SystemExit(f"CPU/GPU profile comparison used an unexpected engine: {case}")
        for phase in ("initial", "final"):
            surface = case["surfaceComparisons"][phase]
            if phase == "initial" and not all(surface[key] for key in (
                    "pointsExactlyEqual", "connectivityExactlyEqual",
                    "cellMaterialIdsExactlyEqual", "levelSetMaterialMapExactlyEqual")):
                raise SystemExit(f"CPU/GPU initial structure differs in profile: {case}")
            for direction in ("cpuToGpuNearestVertices", "gpuToCpuNearestVertices"):
                nearest = surface[direction]
                if (not nearest["allPointsWithinOneTenthGrid"]
                        or nearest["maxNearestDistanceWithinCutoffUm"] is None):
                    raise SystemExit(f"CPU/GPU profile exceeds the recorded geometric tolerance: {case}")
                maximum_vertex_distance = max(maximum_vertex_distance,
                                              float(nearest["maxNearestDistanceWithinCutoffUm"]))
        for flux_row in case.get("fluxMeanComparisons", []):
            value = flux_row.get("relativeGpuMinusCpuPercent")
            if value is None or not math.isfinite(float(value)):
                raise SystemExit(f"CPU/GPU native flux summary is incomplete: {case}")
            relative_flux_differences["2d" if dimension == 2 else "3d"].append(float(value))
    cpu_gpu_summary = {
        "status": "partial-native-cpu-failure", "comparedCases": 5,
        "expectedCases": len(expected_profiles), "cpuNativeFailureCase": cpu_failures[0]["case"],
        "maximumBidirectionalNearestVertexDistanceUm": maximum_vertex_distance,
        "allInitialMeshesExact": True,
        "allComparedFinalMeshesWithinOneTenthGrid": True,
        "relativeGpuMinusCpuMeanFluxPercentRangeByDimension": {
            key: [min(values), max(values)] for key, values in relative_flux_differences.items() if values
        },
        "cpuNativeFailureRerun": {
            "runtimeError": cpu_failure_rerun["runtimeError"],
            "log": cpu_failure_rerun["log"], "guard": cpu_failure_rerun["guard"],
        },
        "evidence": str(MULTIPARTICLE_PATCHED_CPU_GPU_PROFILE.relative_to(ROOT)),
    }
    return {
        "status": "verified", "engine": "GPU_DISK", "engineVersions": ENGINE_VERSIONS,
        "deviceUuid": expected_uuid, "runtimeFingerprint": runtime_fingerprint,
        "verifiedIonSpeciesCount": 8, "verifiedGdsCases": 12, "dimensions": [2, 3],
        "cpuGpuEvolutionTopologyEqual": False,
        "resolvedSolverProfileRechecks": {
            "status": "verified", "count": len(expected_profiles),
            "executionStrategy": "native-multi-ion-v1",
            "comparison": "product-worker exactly matches independent patched-runtime GPU_DISK reference",
            "evidence": str(MULTIPARTICLE_PATCHED_PROFILE_RECHECK.relative_to(ROOT)),
            "browserEvidence": str(MULTIPARTICLE_PATCHED_PROFILE_BROWSER.relative_to(ROOT)),
        },
        "cpuGpuProfileAudit": cpu_gpu_summary,
        "evidence": [str(MULTIPARTICLE_PATCHED_EIGHT_ION.relative_to(ROOT)),
                     str(MULTIPARTICLE_PATCHED_GDS.relative_to(ROOT)),
                     str(MULTIPARTICLE_PATCHED_PROFILE_RECHECK.relative_to(ROOT)),
                     str(MULTIPARTICLE_PATCHED_PROFILE_BROWSER.relative_to(ROOT)),
                     str(MULTIPARTICLE_PATCHED_CPU_GPU_PROFILE.relative_to(ROOT)),
                     str(MULTIPARTICLE_PATCHED_UI_2D.relative_to(ROOT)),
                     str(MULTIPARTICLE_PATCHED_UI_3D.relative_to(ROOT)),
                     str(MULTIPARTICLE_PATCHED_BROWSER_2D.relative_to(ROOT)),
                     str(MULTIPARTICLE_PATCHED_BROWSER_3D.relative_to(ROOT))],
    }


def add_specialized_geometry_rows(rows: list[dict]) -> None:
    """Publish verified installed flux model × preset rows, including Boolean Fin repairs."""
    cases = [json.loads(path.read_text(encoding="utf-8"))
             for path in sorted(SPECIALIZED_GEOMETRIES.glob("*/acceptance.json"))]
    accepted: list[dict] = []
    failed_native: list[dict] = []
    for case in cases:
        # Substrate-specific diagnostic runs are consumed by the material-scope
        # report below, not published as duplicate geometry capability rows.
        if "-bulk-" in str(case.get("name", "")):
            continue
        if case.get("engine") != "GPU_DISK" or case.get("geometry") not in {"plane", "trench", "hole", "fin"}:
            continue
        if case.get("productGeometryReadback", {}).get("automaticFallback"):
            # Keep the published capability row tied to explicit Boolean
            # construction evidence. Automatic fallback cases are reported
            # separately so they cannot duplicate that exact GPU signature.
            continue
        if case.get("status") != "verified":
            if case.get("geometry") == "fin" and case.get("finConstruction", "native") == "native":
                failed_native.append({
                    "case": case.get("name"), "status": case.get("status"),
                    "productReturnCode": case.get("productReturnCode"),
                    "directReturnCode": case.get("directReturnCode"),
                    "productLog": case.get("productLog"), "directLog": case.get("directLog"),
                })
            continue
        comparisons = case.get("comparisons", {})
        if (case.get("productReturnCode") != 0 or case.get("directReturnCode") != 0
                or case.get("productActualEngine") != "GPU_DISK"
                or case.get("directActualEngine") != "GPU_DISK"
                or case.get("productVersions") != ENGINE_VERSIONS
                or case.get("directVersions") != ENGINE_VERSIONS
                or not comparisons or not all(value is True for value in comparisons.values())):
            raise SystemExit(f"GPU specialized geometry acceptance is incomplete: {case.get('name')}")
        project_path = ROOT / case["project"]
        direct_path = ROOT / case["directReference"]
        product_path = project_path.parent / "worker-result.json"
        if not all(path.is_file() for path in (project_path, direct_path, product_path)):
            raise SystemExit(f"GPU specialized geometry evidence is missing: {case.get('name')}")
        project = json.loads(project_path.read_text(encoding="utf-8"))
        worker = json.loads(product_path.read_text(encoding="utf-8"))
        step = project["steps"][0]
        boundary = project["domain"].get("boundaryXY", project["domain"].get("xBoundary"))
        fin_construction = project["geometry"].get("construction", "native") if case.get("geometry") == "fin" else None
        actual = next((entry for entry in worker.get("stepRecords", [])
                       if entry.get("stepId") == case.get("productStepId")), {})
        native_readback = actual.get("actualParameters", {}).get("nativeModel", {})
        if (actual.get("actualEngine") != "GPU_DISK" or worker.get("versions") != ENGINE_VERSIONS
                or native_readback.get("nativeModel") != case.get("nativeModel")):
            raise SystemExit(f"GPU specialized geometry readback differs: {case.get('name')}")
        evidence = {
            "productWorker": str(product_path.relative_to(ROOT)),
            "productGuard": case["productGuard"],
            "directReference": str(direct_path.relative_to(ROOT)),
            "directGuard": case["directGuard"],
            "geometryAcceptance": str((project_path.parent / "acceptance.json").relative_to(ROOT)),
            "comparisons": comparisons,
            "coordinateTolerance": case.get("coordinateTolerance", 0.0),
            "coordinateDeltaMaxAbs": case.get("coordinateDeltaMaxAbs", {"initial": 0.0, "final": 0.0}),
            "versions": ENGINE_VERSIONS,
            "seed": actual.get("actualSeed"),
            "actualEngine": actual.get("actualEngine"),
        }
        row = gpu_row(project, step, f"specialized:{case['model']}", case["dimension"], evidence)
        if case.get("finExtrusionMode") is not None:
            row["finExtrusionMode"] = case["finExtrusionMode"]
            row["finConstruction"] = fin_construction
        rows.append(row)
        accepted.append({"model": case["model"], "geometry": case["geometry"],
                         "dimension": case["dimension"], "finExtrusionMode": case.get("finExtrusionMode"),
                         "boundaryXY": boundary,
                         "finConstruction": fin_construction,
                         "masked": "mask" in row["materialRoles"].get("Mask", []),
                         "acceptance": evidence["geometryAcceptance"]})

    # Every model must have a verified route for all 8 preset/dimension/mode
    # combinations. Native MakeFin crashes are represented by verified Boolean
    # Fin rows instead of being advertised as unsupported.
    models = sorted(spec["id"] for spec in specialized_catalog()["models"] if spec["usesFlux"])
    boundaries = ("reflective", "periodic")
    expected = {(model, geometry, dimension, mode, boundary) for model in models for boundary in boundaries
                for geometry, dimension, mode in (
                    ("plane", 2, None), ("plane", 3, None), ("trench", 2, None),
                    ("trench", 3, None), ("hole", 3, None), ("fin", 2, "direct"),
                    ("fin", 3, "direct"), ("fin", 3, "extruded"))}
    actual = {(item["model"], item["geometry"], item["dimension"], item["finExtrusionMode"], item["boundaryXY"])
              for item in accepted if not item["masked"]}
    missing = sorted(expected - actual)
    if missing:
        raise SystemExit(f"specialized model × preset GPU_DISK coverage is incomplete: {missing[:12]}")
    mask_expected = {(model, 2, "direct", boundary) for model in models for boundary in boundaries} | {
        (model, 3, mode, boundary) for model in models for mode in ("direct", "extruded")
        for boundary in boundaries}
    mask_actual = {(item["model"], item["dimension"], item["finExtrusionMode"], item["boundaryXY"])
                   for item in accepted if item["geometry"] == "fin" and item["masked"]
                   and item["finConstruction"] == "boolean"}
    missing_masks = sorted(mask_expected - mask_actual)
    if missing_masks:
        raise SystemExit(f"masked Boolean Fin GPU_DISK coverage is incomplete: {missing_masks[:12]}")
    boolean_expected = {(model, dimension, mode, masked, boundary) for model in models for boundary in boundaries
                       for dimension, mode in ((2, "direct"), (3, "direct"), (3, "extruded"))
                       for masked in (False, True)}
    boolean_actual = {(item["model"], item["dimension"], item["finExtrusionMode"], item["masked"], item["boundaryXY"])
                      for item in accepted if item["geometry"] == "fin"
                      and item["finConstruction"] == "boolean"}
    if boolean_expected != boolean_actual:
        raise SystemExit(f"specialized Boolean Fin GPU coverage is incomplete: {sorted(boolean_expected - boolean_actual)[:12]}")
    masked_pattern_expected = {(model, geometry, dimension, boundary) for model in models
                               for boundary in boundaries
                               for geometry, dimension in (("trench", 2), ("trench", 3), ("hole", 3))}
    masked_pattern_actual = {(item["model"], item["geometry"], item["dimension"], item["boundaryXY"])
                             for item in accepted if item["geometry"] in {"trench", "hole"} and item["masked"]}
    if masked_pattern_expected != masked_pattern_actual:
        raise SystemExit(f"specialized masked trench/hole GPU coverage is incomplete: {sorted(masked_pattern_expected - masked_pattern_actual)}")

    automatic_fallback_cases = []
    for path in sorted(SPECIALIZED_GEOMETRIES.glob("*-auto-fallback-*/acceptance.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        geometry_readback = case.get("productGeometryReadback", {})
        project = json.loads((ROOT / case["project"]).read_text(encoding="utf-8"))
        if (case.get("status") != "verified" or case.get("productReturnCode") != 0
                or case.get("directReturnCode") != 0 or case.get("productActualEngine") != case.get("engine")
                or case.get("directActualEngine") != case.get("engine")
                or case.get("productVersions") != ENGINE_VERSIONS or case.get("directVersions") != ENGINE_VERSIONS
                or project.get("geometry", {}).get("construction") != "native"
                or geometry_readback.get("requestedConstruction") != "native"
                or geometry_readback.get("construction") != "boolean"
                or not geometry_readback.get("automaticFallback")
                or not all(case.get("comparisons", {}).values())):
            raise SystemExit(f"automatic native MakeFin fallback evidence is incomplete: {case.get('name')}")
        automatic_fallback_cases.append({
            "name": case["name"], "model": case["model"], "dimension": case["dimension"],
            "finExtrusionMode": case["finExtrusionMode"], "engine": case["engine"],
            "masked": float(project.get("geometry", {}).get("maskHeight", 0)) > 0,
            "status": case["status"], "project": case["project"],
            "acceptance": str(path.relative_to(ROOT)), "comparisons": case["comparisons"],
            "productGeometryReadback": geometry_readback,
        })
    expected_fallback = {
        (model, dimension, mode, engine)
        for model in ("sf6o2", "hbro2", "sf6c4f8", "neutralTransport")
        for dimension, mode in ((2, "direct"), (3, "direct"), (3, "extruded"))
        for engine in ("CPU_DISK", "GPU_DISK")
    }
    actual_fallback = {(case["model"], case["dimension"], case["finExtrusionMode"], case["engine"])
                       for case in automatic_fallback_cases if not case["masked"]}
    expected_masked_fallback = {
        (model, dimension, mode, "GPU_DISK")
        for model in ("sf6o2", "hbro2", "sf6c4f8", "neutralTransport")
        for dimension, mode in ((2, "direct"), (3, "direct"), (3, "extruded"))
    }
    actual_masked_fallback = {(case["model"], case["dimension"], case["finExtrusionMode"], case["engine"])
                              for case in automatic_fallback_cases if case["masked"]}
    if actual_fallback != expected_fallback or actual_masked_fallback != expected_masked_fallback:
        raise SystemExit(f"automatic Fin fallback matrix incomplete: unmasked={sorted(expected_fallback - actual_fallback)}, masked={sorted(expected_masked_fallback - actual_masked_fallback)}")

    cpu_reproductions = []
    for path in sorted(SPECIALIZED_GEOMETRIES.glob("*-cpu_disk/acceptance.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        if (case.get("geometry") == "fin" and case.get("finConstruction", "native") == "native"
                and case.get("model") in {"sf6o2", "hbro2", "sf6c4f8", "neutralTransport"}
                and "-rate-" not in case.get("name", "")
                and not case.get("productGeometryReadback", {}).get("automaticFallback")):
            cpu_reproductions.append({"name": case["name"], "model": case["model"],
                                      "dimension": case["dimension"],
                                      "finExtrusionMode": case["finExtrusionMode"],
                                      "status": case["status"], "productReturnCode": case["productReturnCode"],
                                      "directReturnCode": case["directReturnCode"],
                                      "acceptance": str(path.relative_to(ROOT)),
                                      "productLog": case["productLog"], "directLog": case["directLog"]})
    expected_cpu = ({("sf6o2", 2, "direct")} |
                    {(model, dimension, mode)
                     for model in ("hbro2", "sf6c4f8", "neutralTransport")
                     for dimension, mode in ((2, "direct"), (3, "direct"), (3, "extruded"))})
    actual_cpu = {(case["model"], case["dimension"], case["finExtrusionMode"]) for case in cpu_reproductions}
    if actual_cpu != expected_cpu or any(case["status"] == "verified" for case in cpu_reproductions):
        raise SystemExit(f"CPU native MakeFin crash reproductions incomplete: {sorted(expected_cpu - actual_cpu)}")

    periodic_extrusion_fallback_cases = []
    for path in sorted(SPECIALIZED_GEOMETRIES.glob("*-3d-fin-extruded-boolean*-periodic-*/acceptance.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        geometry_readback = case.get("productGeometryReadback", {})
        project = json.loads((ROOT / case["project"]).read_text(encoding="utf-8"))
        if not geometry_readback.get("automaticExtrusionFallback"):
            continue
        if (case.get("status") != "verified" or case.get("productReturnCode") != 0
                or case.get("directReturnCode") != 0 or case.get("productActualEngine") != case.get("engine")
                or case.get("directActualEngine") != case.get("engine")
                or case.get("productVersions") != ENGINE_VERSIONS or case.get("directVersions") != ENGINE_VERSIONS
                or project.get("dimension") != 3 or project.get("domain", {}).get("boundaryXY") != "periodic"
                or project.get("geometry", {}).get("extrusion") is None
                or geometry_readback.get("extrusionRoute") != "direct-stencil"
                or not all(case.get("comparisons", {}).values())):
            raise SystemExit(f"periodic extruded Fin fallback evidence is incomplete: {case.get('name')}")
        periodic_extrusion_fallback_cases.append({
            "name": case["name"], "model": case["model"], "engine": case["engine"],
            "masked": float(project.get("geometry", {}).get("maskHeight", 0)) > 0,
            "status": case["status"], "project": case["project"],
            "acceptance": str(path.relative_to(ROOT)), "comparisons": case["comparisons"],
            "productGeometryReadback": geometry_readback,
        })
    expected_periodic_routes = {
        (model, engine, masked)
        for model in ("sf6o2", "hbro2", "sf6c4f8", "neutralTransport")
        for engine, masked in (("CPU_DISK", False), ("GPU_DISK", False), ("GPU_DISK", True))
    }
    actual_periodic_routes = {(case["model"], case["engine"], case["masked"])
                              for case in periodic_extrusion_fallback_cases}
    if actual_periodic_routes != expected_periodic_routes:
        raise SystemExit(f"periodic extruded Fin fallback matrix incomplete: {sorted(expected_periodic_routes - actual_periodic_routes)}")

    periodic_crashes = []
    crash_root = SPECIALIZED_GEOMETRIES / "known-crashes-periodic-extrusion"
    for path in sorted(crash_root.glob("*/acceptance.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        archive_dir = path.parent
        product_guard_path = archive_dir / "product.guard.json"
        direct_guard_path = archive_dir / "direct.guard.json"
        if not product_guard_path.is_file() or not direct_guard_path.is_file():
            raise SystemExit(f"periodic Fin crash guards are missing: {path.parent.name}")
        product_guard = json.loads(product_guard_path.read_text(encoding="utf-8"))
        direct_guard = json.loads(direct_guard_path.read_text(encoding="utf-8"))
        periodic_crashes.append({
            "name": case["name"], "model": case["model"], "engine": case["engine"],
            # The fixture runner reports the shell's 8-bit status (245 for
            # SIGSEGV); the bounded-child record retains the native signal as
            # -11. Publish both so the diagnosis is explicit and auditable.
            "productReturnCode": product_guard.get("returnCode"),
            "directReturnCode": direct_guard.get("returnCode"),
            "productRunnerReturnCode": case["productReturnCode"],
            "directRunnerReturnCode": case["directReturnCode"],
            "productTimedOut": product_guard.get("timedOut"),
            "directTimedOut": direct_guard.get("timedOut"),
            "acceptance": str(path.relative_to(ROOT)),
            "productLog": str((archive_dir / "product.log").relative_to(ROOT)),
            "directLog": str((archive_dir / "direct.log").relative_to(ROOT)),
            "productGuard": str(product_guard_path.relative_to(ROOT)),
            "directGuard": str(direct_guard_path.relative_to(ROOT)),
        })
    expected_crashes = {(model, engine) for model in ("sf6o2", "hbro2", "sf6c4f8", "neutralTransport")
                        for engine in ("CPU_DISK", "GPU_DISK")}
    actual_crashes = {(case["model"], case["engine"]) for case in periodic_crashes}
    if actual_crashes != expected_crashes or any(case["productReturnCode"] != -11 or case["directReturnCode"] != -11
                                                 or case["productRunnerReturnCode"] != 245
                                                 or case["directRunnerReturnCode"] != 245
                                                 or case["productTimedOut"] is not False
                                                 or case["directTimedOut"] is not False
                                                 for case in periodic_crashes):
        raise SystemExit("periodic extruded Fin crash archive is incomplete")

    report = {
        "status": "passed", "versions": ENGINE_VERSIONS,
        "modelPresetCombinationsExpected": len(expected),
        "modelPresetCombinationsVerified": len(expected),
        "maskedBooleanFinCombinationsExpected": len(mask_expected),
        "maskedBooleanFinCombinationsVerified": len(mask_expected),
        "specializedMaskedTrenchHoleCombinationsExpected": len(masked_pattern_expected),
        "specializedMaskedTrenchHoleCombinationsVerified": len(masked_pattern_expected),
        "specializedBooleanFinCombinationsExpected": len(boolean_expected),
        "specializedBooleanFinCombinationsVerified": len(boolean_expected),
        "verifiedCases": accepted,
        "knownNativeMakeFinFailures": failed_native,
        "automaticFinFallbackCases": automatic_fallback_cases,
        "periodicFinExtrusionFallbackCases": periodic_extrusion_fallback_cases,
        "knownPeriodicFinExtrusionFailures": periodic_crashes,
        "periodicFinExtrusionDiagnosis": {
            "models": ["hbro2", "neutralTransport", "sf6c4f8", "sf6o2"],
            "summary": "The installed ViennaPS 4.6.2 d2.MakeFin/GeometryFactory plus Extrude route reproducibly SIGSEGVs for these four models on periodic 3D Fin domains after the initial geometry and flux coverages are created; CPU_DISK and GPU_DISK product workers and independent direct calls all reproduce it. The worker now constructs the same requested y-range with a ViennaPS d3.GeometryFactory Boolean box stencil. The d3 route matches independent reference meshes and process results on both engines, with and without the Fin mask.",
            "diagnosisLimit": "The native stack does not expose symbols for the C++ crash site. The failing input, both-engine logs, and bounded-child records are retained with the four crash cases.",
        },
        "nativeFailureDiagnosis": {
            "models": ["hbro2", "neutralTransport", "sf6c4f8", "sf6o2"],
            "summary": "The four listed model × native MakeFin combinations reproducibly exit with SIGSEGV in ViennaPS 4.6.2 on GPU_DISK, with matching independent direct calls. CPU_DISK reproduced 10 standard cases including every mode for HBrO2, SF6/C4F8 and NeutralTransport plus SF6/O2 2D. Automatic fallback to Boolean Fin construction using installed ViennaPS MakePlane, GeometryFactory and Domain Boolean/material level-set APIs succeeds and matches the product worker on both engines across all 12 model/mode signatures.",
            "cpuReproduction": "docs/stage9-evidence/gpu-coverage-audit/specialized-geometries/sf6o2-2d-fin-direct-gpu_disk/cpu-gdb.log",
            "cpuReproductionCases": cpu_reproductions,
            "diagnosisLimit": "The installed ViennaPS 4.6.2 native stack does not expose useful symbols past the extension boundary, so the C++ crash source is not identified. The user-facing automatic Boolean Fin fallback is verified on CPU_DISK and GPU_DISK.",
        },
    }
    GEOMETRY_COVERAGE_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def add_generic_boolean_fin_rows(rows: list[dict]) -> dict[str, int]:
    """Add Boolean Fin rows for the generic SingleParticle/MultiParticle models."""
    files = sorted(GEOMETRY_PRESETS.parent.glob("singleParticle-*fin-*-boolean*/acceptance.json"))
    files += sorted(GEOMETRY_PRESETS.parent.glob("multiParticle-*fin-*-boolean*/acceptance.json"))
    cases = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    accepted: list[dict] = []
    for case in cases:
        comparisons = case.get("comparisons", {})
        if (case.get("status") != "verified" or case.get("engine") != "GPU_DISK"
                or case.get("productReturnCode") != 0 or case.get("directReturnCode") != 0
                or case.get("productActualEngine") != "GPU_DISK"
                or case.get("directActualEngine") != "GPU_DISK"
                or case.get("productVersions") != ENGINE_VERSIONS
                or case.get("directVersions") != ENGINE_VERSIONS
                or not comparisons or not all(comparisons.values())):
            raise SystemExit(f"generic Boolean Fin GPU evidence is incomplete: {case.get('name')}")
        project_path = ROOT / case["project"]
        direct_path = ROOT / case["directReference"]
        product_path = project_path.parent / "worker-result.json"
        if not all(path.is_file() for path in (project_path, direct_path, product_path)):
            raise SystemExit(f"generic Boolean Fin evidence is missing: {case.get('name')}")
        project = json.loads(project_path.read_text(encoding="utf-8"))
        if project.get("domain", {}).get("boundaryXY", project.get("domain", {}).get("xBoundary")) != "reflective":
            continue
        # The directory also contains the exhaustive material sweep. Those
        # runs prove material registry coverage below; they must not become
        # duplicate geometry rows or inflate the representative matrix.
        mode = case.get("finExtrusionMode") or "direct"
        mask_tag = "mask-0.4-" if float(project["geometry"].get("maskHeight", 0)) > 0 else ""
        expected_name = (f"{case['model']}-{case['dimension']}d-fin-{mode}-boolean-"
                         f"{mask_tag}{case['operation']}-gpu_disk")
        if case.get("name") != expected_name:
            continue
        worker = json.loads(product_path.read_text(encoding="utf-8"))
        step = project["steps"][0]
        product_step = next((item for item in worker.get("stepRecords", [])
                             if item.get("stepId") == case.get("productStepId")), {})
        if worker.get("versions") != ENGINE_VERSIONS or product_step.get("actualEngine") != "GPU_DISK":
            raise SystemExit(f"generic Boolean Fin worker readback differs: {case.get('name')}")
        evidence = {
            "productWorker": str(product_path.relative_to(ROOT)),
            "productGuard": case["productGuard"],
            "directReference": str(direct_path.relative_to(ROOT)),
            "directGuard": case["directGuard"],
            "geometryAcceptance": str((project_path.parent / "acceptance.json").relative_to(ROOT)),
            "comparisons": comparisons,
            "versions": ENGINE_VERSIONS,
            "seed": product_step.get("actualSeed"),
            "actualEngine": product_step.get("actualEngine"),
        }
        rows.append(gpu_row(project, step, case["model"], case["dimension"], evidence))
        accepted.append({"model": case["model"], "dimension": case["dimension"],
                         "mode": case["finExtrusionMode"], "operation": case["operation"],
                         "masked": bool("mask" in gpu_project_signature(project, step)["material_roles"].get("Mask", [])),
                         "acceptance": evidence["geometryAcceptance"]})

    expected = {(model, dimension, mode, operation, masked)
                for model in ("singleParticle", "multiParticle")
                for dimension, mode in ((2, "direct"), (3, "direct"), (3, "extruded"))
                for operation in ("etch", "deposit") for masked in (False, True)}
    actual = {(item["model"], item["dimension"], item["mode"], item["operation"], item["masked"])
              for item in accepted}
    missing = sorted(expected - actual)
    if missing:
        raise SystemExit(f"generic Boolean Fin GPU coverage is incomplete: {missing}")
    if len(accepted) != len(expected):
        raise SystemExit(f"generic Boolean Fin GPU representative rows are duplicated: expected {len(expected)}, got {len(accepted)}")
    report = json.loads(GEOMETRY_COVERAGE_REPORT.read_text(encoding="utf-8"))
    report["genericBooleanFinCombinationsExpected"] = 12
    report["genericBooleanFinCombinationsVerified"] = 12
    report["genericMaskedBooleanFinCombinationsExpected"] = 12
    report["genericMaskedBooleanFinCombinationsVerified"] = 12
    report["specializedBooleanFinCombinationsExpected"] = sum(
        item.get("finConstruction") == "boolean" for item in report["verifiedCases"])
    report["specializedBooleanFinCombinationsVerified"] = sum(
        item.get("finConstruction") == "boolean" for item in report["verifiedCases"])
    report["totalBooleanFinCombinationsVerified"] = report["specializedBooleanFinCombinationsVerified"] + len(accepted)
    report["genericBooleanFinCases"] = accepted
    GEOMETRY_COVERAGE_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"unmasked": sum(not case["masked"] for case in accepted),
            "masked": sum(case["masked"] for case in accepted)}


def add_generic_masked_pattern_rows(rows: list[dict]) -> int:
    """Add verified masked trench and hole routes for generic particle models."""
    files = []
    for model in ("singleParticle", "multiParticle"):
        for geometry in ("trench", "hole"):
            files.extend(sorted(GEOMETRY_PRESETS.parent.glob(f"{model}-*-{geometry}-*-mask-*/acceptance.json")))
    cases = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    accepted: set[tuple[str, int, str | None, str, str]] = set()
    for case in cases:
        if case.get("engine") != "GPU_DISK":
            continue
        comparisons = case.get("comparisons", {})
        if (case.get("status") != "verified"
                or case.get("productReturnCode") != 0 or case.get("directReturnCode") != 0
                or case.get("productActualEngine") != "GPU_DISK"
                or case.get("directActualEngine") != "GPU_DISK"
                or case.get("productVersions") != ENGINE_VERSIONS
                or case.get("directVersions") != ENGINE_VERSIONS
                or not comparisons or not all(comparisons.values())):
            raise SystemExit(f"generic masked pattern GPU evidence is incomplete: {case.get('name')}")
        project_path = ROOT / case["project"]
        direct_path = ROOT / case["directReference"]
        product_path = project_path.parent / "worker-result.json"
        if not all(path.is_file() for path in (project_path, direct_path, product_path)):
            raise SystemExit(f"generic masked pattern evidence is missing: {case.get('name')}")
        project = json.loads(project_path.read_text(encoding="utf-8"))
        if project.get("domain", {}).get("boundaryXY", project.get("domain", {}).get("xBoundary")) != "reflective":
            continue
        expected_name = (f"{case['model']}-{case['dimension']}d-{case['geometry']}-direct-"
                         f"mask-0.4-{case['operation']}-gpu_disk")
        if case.get("name") != expected_name:
            continue
        worker = json.loads(product_path.read_text(encoding="utf-8"))
        step = project["steps"][0]
        product_step = next((item for item in worker.get("stepRecords", [])
                             if item.get("stepId") == case.get("productStepId")), {})
        if worker.get("versions") != ENGINE_VERSIONS or product_step.get("actualEngine") != "GPU_DISK":
            raise SystemExit(f"generic masked pattern worker readback differs: {case.get('name')}")
        evidence = {
            "productWorker": str(product_path.relative_to(ROOT)),
            "productGuard": case["productGuard"],
            "directReference": str(direct_path.relative_to(ROOT)),
            "directGuard": case["directGuard"],
            "geometryAcceptance": str((project_path.parent / "acceptance.json").relative_to(ROOT)),
            "comparisons": comparisons,
            "versions": ENGINE_VERSIONS,
            "seed": product_step.get("actualSeed"),
            "actualEngine": product_step.get("actualEngine"),
        }
        rows.append(gpu_row(project, step, case["model"], case["dimension"], evidence))
        accepted.add((case["geometry"], case["dimension"], case.get("finExtrusionMode"),
                      case["model"], case["operation"]))

    expected = {(geometry, dimension, None, model, operation)
                for geometry, dimension in (("trench", 2), ("trench", 3), ("hole", 3))
                for model in ("singleParticle", "multiParticle")
                for operation in ("etch", "deposit")}
    if accepted != expected:
        raise SystemExit(f"generic masked trench/hole GPU coverage is incomplete: {sorted(expected - accepted)}")
    if len(accepted) != len(expected):
        raise SystemExit(f"generic masked trench/hole representative rows are duplicated: expected {len(expected)}, got {len(accepted)}")
    report = json.loads(GEOMETRY_COVERAGE_REPORT.read_text(encoding="utf-8"))
    report["genericMaskedTrenchHoleCombinationsExpected"] = len(expected)
    report["genericMaskedTrenchHoleCombinationsVerified"] = len(expected)
    GEOMETRY_COVERAGE_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(accepted)


def periodic_signature(row: dict) -> tuple:
    return tuple(row.get(name) for name in ("dimension", "model", "operation", "boundaryXY", "engine")) + (
        tuple(row["geometryKinds"]), tuple(row.get("patternKinds", [])), tuple(row["materials"]),
        json.dumps(row["materialRoles"], sort_keys=True), json.dumps(row["materialBindings"], sort_keys=True),
        json.dumps(row.get("speciesSignature"), sort_keys=True), row.get("finExtrusionMode"),
        row.get("finConstruction"))


def _verify_material_scope_summary(path: Path, expected_cases: int) -> dict:
    summary = json.loads(path.read_text(encoding="utf-8"))
    if (not summary.get("passed") or summary.get("engine") != "GPU_DISK"
            or summary.get("installedVersions") != ENGINE_VERSIONS
            or summary.get("caseCount") != expected_cases
            or summary.get("expectedCaseCount") != expected_cases
            or len(summary.get("rows", [])) != expected_cases):
        raise SystemExit(f"GPU material-scope summary is incomplete: {path.relative_to(ROOT)}")
    for case in summary["rows"]:
        if (case.get("status") != "verified" or case.get("engine") != "GPU_DISK"
                or case.get("productActualEngine") != "GPU_DISK" or case.get("directActualEngine") != "GPU_DISK"
                or case.get("productVersions") != ENGINE_VERSIONS or case.get("directVersions") != ENGINE_VERSIONS
                or case.get("productReturnCode") != 0 or case.get("directReturnCode") != 0
                or not case.get("comparisons") or not all(value is True for value in case["comparisons"].values())):
            raise SystemExit(f"unverified GPU material-scope row in {path.name}: {case.get('name')}")
        for field in ("productGuard", "directGuard"):
            guard_path = ROOT / case[field]
            if not guard_path.is_file():
                raise SystemExit(f"material-scope guard is missing: {case.get(field)}")
            guard = json.loads(guard_path.read_text(encoding="utf-8"))
            if (guard.get("returnCode") != 0 or guard.get("timedOut") or guard.get("rssLimited")
                    or guard.get("remainingOwnedGroupMembers")):
                raise SystemExit(f"material-scope bounded child did not exit cleanly: {case.get(field)}")
    return summary


def add_specialized_substrate_scope(rows: list[dict]) -> dict:
    """Attach only the measured 2D reflective plane substrate sweep to flux models."""
    summary_path = SPECIALIZED_MATERIAL_SCOPE / "selectable-substrates-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "docs/materials-catalog.json").read_text(encoding="utf-8"))["materials"]
    builtin_ids = {str(item["name"]): int(item["id"]) for item in catalog}
    materials = sorted(builtin_ids)
    models = sorted(spec["id"] for spec in specialized_catalog()["models"] if spec["usesFlux"])
    if (summary.get("status") != "passed" or summary.get("engine") != "GPU_DISK"
            or summary.get("engineVersion") != "4.6.2" or summary.get("dimension") != 2
            or summary.get("geometry") != "plane" or summary.get("caseCount") != 130
            or summary.get("verifiedCount") != 130
            or sorted(summary.get("substrateMaterials", [])) != materials
            or sorted(summary.get("models", [])) != models):
        raise SystemExit("specialized GPU substrate sweep summary is incomplete")

    by_model: dict[str, list[dict]] = {model: [] for model in models}
    for material_row in summary["rows"]:
        material = material_row.get("material")
        case_summary_path = ROOT / material_row.get("summary", "")
        if (material not in builtin_ids or material_row.get("runnerReturnCode") != 0
                or material_row.get("caseCount") != len(models)
                or material_row.get("verifiedCount") != len(models) or not case_summary_path.is_file()):
            raise SystemExit(f"specialized GPU substrate sweep is incomplete for {material!r}")
        case_summary = json.loads(case_summary_path.read_text(encoding="utf-8"))
        cases = case_summary.get("rows", [])
        if case_summary.get("status") != "passed" or not case_summary.get("passed") or len(cases) != len(models):
            raise SystemExit(f"specialized GPU substrate cases failed for {material}")
        seen: set[str] = set()
        for case in cases:
            model = case.get("model")
            if (model not in by_model or model in seen or case.get("status") != "verified"
                    or case.get("engine") != "GPU_DISK" or case.get("dimension") != 2
                    or case.get("geometry") != "plane" or case.get("productActualEngine") != "GPU_DISK"
                    or case.get("directActualEngine") != "GPU_DISK"
                    or case.get("productVersions") != ENGINE_VERSIONS
                    or case.get("directVersions") != ENGINE_VERSIONS
                    or case.get("productReturnCode") != 0 or case.get("directReturnCode") != 0
                    or not case.get("comparisons")
                    or not all(value is True for value in case["comparisons"].values())):
                raise SystemExit(f"unverified specialized GPU substrate case: {material}/{model}")
            cuda = case.get("productActualParameters", {}).get("cudaDeviceReadback", {})
            process = cuda.get("process", {})
            post = cuda.get("afterProcessApply", {})
            device_verified_after_apply = (
                post.get("readbackStatus") == "verified"
                and post.get("uuid") == cuda.get("uuid")
                and post.get("synchronizeStatus") == 0
            )
            legacy_process_verification = (
                process.get("processListed") is True
                and process.get("gpuUuid") == cuda.get("uuid")
            )
            process_visibility_consistent = (
                (process.get("processListed") is True and process.get("processListStatus", "verified") == "verified"
                 and process.get("gpuUuid") == cuda.get("uuid"))
                or (process.get("processListed") is False
                    and process.get("processListStatus") in {"not-observed", "unavailable"}
                    and process.get("expectedGpuUuid") == cuda.get("uuid"))
            )
            if (cuda.get("readbackStatus") != "verified"
                    or cuda.get("uuid") != "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d"
                    or cuda.get("name") != "NVIDIA GeForce RTX 3060 Laptop GPU"
                    or not (device_verified_after_apply or legacy_process_verification)
                    or not process_visibility_consistent):
                raise SystemExit(f"specialized GPU substrate case did not run on the RTX 3060: {material}/{model}")
            seen.add(model)
            project_path = ROOT / case.get("project", "")
            direct_path = ROOT / case.get("directReference", "")
            worker_path = project_path.parent / "worker-result.json"
            if not all(path.is_file() for path in (project_path, direct_path, worker_path)):
                raise SystemExit(f"specialized GPU substrate output is missing: {material}/{model}")
            project = json.loads(project_path.read_text(encoding="utf-8"))
            step = project["steps"][0]
            signature = gpu_project_signature(project, step)
            if (project.get("dimension") != 2 or project["geometry"].get("kind") != "plane"
                    or project["geometry"].get("substrateMaterial") != material
                    or project["domain"].get("xBoundary", "reflective") != "reflective"
                    or step.get("nativeModel") != model):
                raise SystemExit(f"specialized GPU substrate input label differs: {material}/{model}")
            for field in ("productGuard", "directGuard"):
                guard_path = ROOT / case.get(field, "")
                if not guard_path.is_file():
                    raise SystemExit(f"specialized GPU substrate guard is missing: {material}/{model}/{field}")
                guard = json.loads(guard_path.read_text(encoding="utf-8"))
                if (guard.get("returnCode") != 0 or guard.get("timedOut") or guard.get("rssLimited")
                        or guard.get("remainingOwnedGroupMembers")):
                    raise SystemExit(f"specialized GPU substrate child did not exit cleanly: {material}/{model}/{field}")
            by_model[model].append({"material": material, "project": project, "signature": signature,
                                    "case": case, "caseSummary": str(case_summary_path.relative_to(ROOT)),
                                    "worker": str(worker_path.relative_to(ROOT))})
        if seen != set(models):
            raise SystemExit(f"specialized GPU substrate model list is incomplete for {material}")

    scope_by_model: dict[str, dict] = {}
    report_rows: list[dict] = []
    for model, cases in by_model.items():
        if len(cases) != len(materials):
            raise SystemExit(f"specialized GPU substrate count differs for {model}")
        cases.sort(key=lambda item: item["material"])
        signatures = [item["signature"] for item in cases]
        operations = {item["project"]["steps"][0]["operation"] for item in cases}
        bindings = [signature["material_bindings"] for signature in signatures]
        if len(operations) != 1 or any(sorted(value) != sorted(bindings[0]) for value in bindings[1:]):
            raise SystemExit(f"specialized GPU binding keys changed during substrate sweep: {model}")
        operation = next(iter(operations))
        binding_roles: dict[str, str] = {}
        if model == "neutralTransport":
            if any(value != {"etchFrontMaterial": item["material"]} for value, item in zip(bindings, cases)):
                raise SystemExit("NeutralTransport etch front does not follow the selected substrate")
            binding_roles = {"etchFrontMaterial": "bulk"}
            static_bindings: dict[str, str] = {}
        else:
            if any(value != bindings[0] for value in bindings[1:]):
                raise SystemExit(f"specialized GPU binding values changed during substrate sweep: {model}")
            static_bindings = bindings[0]
        allowed_sets = sorted({tuple(signature["materials"]) for signature in signatures})
        bulk_ids = sorted({builtin_ids[item["material"]] for item in cases})
        scope = {
            "kind": "viennaps-native-material-registry-v1", "engineVersion": "4.6.2",
            "builtInLegacyIds": sorted(builtin_ids.values()), "customLegacyIdRange": [177, 240],
            "boundaries": ["reflective"], "roleKindsByGeometry": {"plane": [["bulk"]]},
            "materialLegacyIdsByRoleByGeometry": {"plane": {"bulk": bulk_ids}},
            "bindingKeysByOperation": {operation: sorted(bindings[0])},
            "bindingValuesByOperation": {operation: static_bindings},
            "bindingRolesByOperation": {operation: binding_roles},
            "allowedMaterialSets": [list(values) for values in allowed_sets],
            "intrinsicMaterials": ["Polymer"] if any("Polymer" in values for values in allowed_sets) else [],
            "evidence": [str((SPECIALIZED_MATERIAL_SCOPE / "specialized-substrate-capability-report.json").relative_to(ROOT))],
        }
        scope_by_model[model] = {"operation": operation, "scope": scope}
        report_rows.append({
            "model": model, "dimension": 2, "geometry": "plane", "boundaryXY": "reflective",
            "operation": operation, "substrateMaterials": materials, "bulkLegacyIds": bulk_ids,
            "allowedMaterialSets": scope["allowedMaterialSets"],
            "bindingKeys": sorted(bindings[0]), "bindingValues": static_bindings,
            "bindingRoles": binding_roles,
            "bindingValuesBySubstrate": ({item["material"]: item["signature"]["material_bindings"]
                                           for item in cases} if binding_roles else {}),
            "verifiedCases": [{"material": item["material"], "project": item["case"]["project"],
                               "directReference": item["case"]["directReference"],
                               "productWorker": item["worker"],
                               "productGuard": item["case"]["productGuard"],
                               "directGuard": item["case"]["directGuard"],
                               "cudaDeviceReadback": item["case"]["productActualParameters"]["cudaDeviceReadback"],
                               "comparisons": item["case"]["comparisons"],
                               "caseSummary": item["caseSummary"]} for item in cases],
        })

    attached = 0
    for row in rows:
        model_name = row.get("model", "")
        if (not model_name.startswith("specialized:") or row.get("dimension") != 2
                or row.get("boundaryXY") != "reflective" or row.get("geometryKinds") != ["plane"]):
            continue
        native_model = model_name.split(":", 1)[1]
        entry = scope_by_model.get(native_model)
        if entry is None or row.get("operation") != entry["operation"]:
            continue
        row["materialScope"] = entry["scope"]
        attached += 1
    if attached != len(models):
        raise SystemExit(f"specialized GPU substrate scopes attached to {attached}/{len(models)} plane rows")

    report = {"status": "passed", "engine": "GPU_DISK", "engineVersions": ENGINE_VERSIONS,
              "dimension": 2, "geometry": "plane", "boundaryXY": "reflective",
              "device": {"name": "NVIDIA GeForce RTX 3060 Laptop GPU",
                         "uuid": "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d",
                         "pciBusId": "0000:01:00.0"},
              "materialCount": len(materials), "modelCount": len(models),
              "caseCount": summary["caseCount"], "verifiedCount": summary["verifiedCount"],
              "sourceSummary": str(summary_path.relative_to(ROOT)), "rows": report_rows}
    report_path = SPECIALIZED_MATERIAL_SCOPE / "specialized-substrate-capability-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _material_ids_for_scope(summary: dict, builtin_ids: dict[str, int], geometry_kinds: set[str]) -> dict:
    role_ids: dict[str, dict[str, set[int]]] = {
        geometry: {"bulk": set(), "mask": set(), "film": set()} for geometry in geometry_kinds
    }
    for case in summary["rows"]:
        if case.get("geometry") not in geometry_kinds:
            continue
        project = json.loads((ROOT / case["project"]).read_text(encoding="utf-8"))
        geometry = project["geometry"]
        kind = geometry["kind"]
        substrate = geometry["substrateMaterial"]
        if substrate in builtin_ids:
            role_ids[kind]["bulk"].add(builtin_ids[substrate])
        if kind in {"trench", "hole", "fin"} and float(geometry.get("maskHeight", 0)) > 0:
            mask = geometry.get("maskMaterial", "Mask")
            if mask in builtin_ids:
                role_ids[kind]["mask"].add(builtin_ids[mask])
        step = project["steps"][0]
        deposit = step.get("depositMaterial")
        if step.get("operation") == "deposit" and deposit in builtin_ids:
            role_ids[kind]["film"].add(builtin_ids[deposit])
    return {geometry: {role: sorted(ids) for role, ids in roles.items() if ids}
            for geometry, roles in role_ids.items()}


def add_generic_material_scopes(rows: list[dict]) -> dict:
    """Attach generic geometry/material scopes covered by direct GPU probes."""
    builtin_rows = json.loads((ROOT / "docs/materials-catalog.json").read_text(encoding="utf-8"))["materials"]
    builtin_ids = {item["name"]: int(item["id"]) for item in builtin_rows}
    all_builtin_ids = sorted(builtin_ids.values())
    summaries = {
        "builtins": (MATERIAL_SCOPE / "summary-builtins-fin-gpu_disk.json", 260),
        "holes": (MATERIAL_SCOPE / "summary-holes-gpu_disk.json", 52),
        "fins": (MATERIAL_SCOPE / "summary-fins-gpu_disk.json", 624),
        "custom": (MATERIAL_SCOPE / "summary-custom-gpu_disk.json", 12),
    }
    verified = {key: _verify_material_scope_summary(path, count)
                for key, (path, count) in summaries.items()}
    periodic_path = MATERIAL_SCOPE / "summary-all-fin-periodic-gpu_disk.json"
    periodic = _verify_material_scope_summary(periodic_path, 948)
    geometry_roles_by_boundary = {
        "reflective": _material_ids_for_scope(verified["builtins"], builtin_ids, {"plane", "trench"}),
        "periodic": _material_ids_for_scope(periodic, builtin_ids, {"plane", "trench", "hole", "fin"}),
    }
    geometry_roles_by_boundary["reflective"].update(
        _material_ids_for_scope(verified["holes"], builtin_ids, {"hole"}))
    geometry_roles_by_boundary["reflective"].update(
        _material_ids_for_scope(verified["fins"], builtin_ids, {"fin"}))
    expected_role_ids = set(all_builtin_ids)
    required_roles = {"plane": ("bulk", "film"), "trench": ("bulk", "mask", "film"),
                      "hole": ("bulk", "mask", "film"), "fin": ("bulk", "mask", "film")}
    for boundary, geometry_roles in geometry_roles_by_boundary.items():
        for geometry, roles in required_roles.items():
            for role in roles:
                if set(geometry_roles.get(geometry, {}).get(role, [])) != expected_role_ids:
                    raise SystemExit(f"{boundary} built-in material ID sweep is incomplete for {geometry}/{role}")

    def custom_coverage(summary: dict) -> tuple[set[tuple[int, str]], dict[str, bool]]:
        roles: set[tuple[int, str]] = set()
        tables = {"singleParticle": False, "multiParticle": False}
        for case in summary["rows"]:
            project = json.loads((ROOT / case["project"]).read_text(encoding="utf-8"))
            definitions = project.get("materialDefinitions", [])
            id_by_name = {row["name"]: int(row["id"]) for row in definitions}
            geometry = project["geometry"]
            step = project["steps"][0]
            for role, name in (("bulk", geometry.get("substrateMaterial")),
                               ("mask", geometry.get("maskMaterial") if float(geometry.get("maskHeight", 0)) > 0 else None),
                               ("film", step.get("depositMaterial"))):
                if name in id_by_name:
                    roles.add((id_by_name[name], role))
            if len(definitions) == 64:
                names = {row["name"] for row in definitions}
                if step.get("model") == "singleParticle":
                    tables["singleParticle"] |= names.issubset(set(step.get("materialRates", {})))
                elif step.get("model") == "multiParticle":
                    neutral = next((row for row in step.get("species", []) if row.get("kind") == "neutral"), {})
                    tables["multiParticle"] |= (
                        names.issubset(set(step.get("rateCoefficients", {}).get("byMaterial", {})))
                        and names.issubset(set(neutral.get("stickingProbabilityByMaterial", {}))))
        return roles, tables

    custom_roles, full_custom_tables = custom_coverage(verified["custom"])
    periodic_custom_roles, periodic_custom_tables = custom_coverage(periodic)
    expected_custom_roles = {(177, "bulk"), (240, "bulk"), (177, "film"), (240, "mask")}
    if (not expected_custom_roles.issubset(custom_roles) or not all(full_custom_tables.values())
            or not expected_custom_roles.issubset(periodic_custom_roles)
            or not all(periodic_custom_tables.values())):
        raise SystemExit("custom native material edge IDs or 64-entry process tables are not covered on both boundaries")

    summary_paths = {key: str(path.relative_to(ROOT)) for key, (path, _) in summaries.items()}
    summary_paths["periodic"] = str(periodic_path.relative_to(ROOT))
    role_kinds = {
        "plane": [["bulk"]],
        "trench": [["bulk"], ["bulk", "mask"]],
        "hole": [["bulk"], ["bulk", "mask"]],
        "fin": [["bulk"], ["bulk", "mask"]],
    }
    attached: list[dict] = []
    for row in rows:
        if (row.get("status") != "verified" or row.get("engine") != "GPU_DISK"
                or row.get("model") not in {"singleParticle", "multiParticle"}
                or row.get("boundaryXY") not in geometry_roles_by_boundary
                or len(row.get("geometryKinds", [])) != 1):
            continue
        boundary = row["boundaryXY"]
        geometry = row["geometryKinds"][0]
        if geometry not in role_kinds or (geometry == "hole" and row.get("dimension") != 3):
            continue
        if boundary == "periodic":
            material_evidence = [summary_paths["periodic"]]
        else:
            material_evidence = [summary_paths["custom"]]
            if geometry in {"plane", "trench"}:
                material_evidence.append(summary_paths["builtins"])
            elif geometry == "hole":
                material_evidence.append(summary_paths["holes"])
            else:
                material_evidence.append(summary_paths["fins"])
        row["materialScope"] = {
            "kind": "viennaps-native-material-registry-v1",
            "engineVersion": "4.6.2",
            "builtInLegacyIds": all_builtin_ids,
            "customLegacyIdRange": [177, 240],
            "boundaries": [boundary],
            "roleKindsByGeometry": {geometry: role_kinds[geometry]},
            "bindingKeysByOperation": {"etch": [], "deposit": ["depositMaterial"]},
            "evidence": material_evidence,
        }
        if row.get("model") == "multiParticle":
            row["speciesScope"] = {"kind": "material-tables-v1", "evidence": material_evidence}
        attached.append({"dimension": row["dimension"], "model": row["model"],
                         "operation": row["operation"], "geometry": geometry,
                         "finExtrusionMode": row.get("finExtrusionMode"),
                         "finConstruction": row.get("finConstruction"), "materialScope": row["materialScope"],
                         "speciesScope": row.get("speciesScope")})

    report = {
        "status": "passed", "engine": "GPU_DISK", "engineVersions": ENGINE_VERSIONS,
        "builtInMaterials": [{"name": item["name"], "legacyId": int(item["id"])} for item in builtin_rows],
        "builtInMaterialRoleCoverage": geometry_roles_by_boundary,
        "customNativeIdRange": [177, 240],
        "customNativeIdRoleCoverage": {
            "reflective": [list(item) for item in sorted(custom_roles)],
            "periodic": [list(item) for item in sorted(periodic_custom_roles)],
        },
        "customTableCoverage": {"reflective": full_custom_tables, "periodic": periodic_custom_tables},
        "caseCounts": {**{key: value["caseCount"] for key, value in verified.items()},
                       "periodic-all": periodic["caseCount"]},
        "summaryEvidence": summary_paths,
        "scopedCapabilityRows": attached,
        "scopedCapabilityRowCount": len(attached),
        "scope": "reflective and periodic generic plane/trench/hole/Fin geometry with installed 4.6.2 built-in/custom native material IDs; MultiParticle material override/sticking tables are independently direct-compared",
    }
    MATERIAL_SCOPE_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def add_periodic_generic_geometry_rows(rows: list[dict]) -> dict[str, int]:
    """Publish the full periodic generic-model geometry and Fin-route matrix."""
    paths = sorted(GEOMETRY_PRESETS.parent.glob("*-periodic-*/acceptance.json"))
    candidates = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in paths]
    # Prefer the real native request with its product-selected fallback when
    # it has the same effective GPU signature as an explicit Boolean request.
    candidates.sort(key=lambda item: (
        item[1].get("finConstruction") == "boolean", item[1].get("name", "")))
    local_keys: set[tuple] = set()
    accepted: list[dict] = []
    automatic_fin_fallbacks: list[dict] = []
    for acceptance_path, case in candidates:
        if case.get("model") not in {"singleParticle", "multiParticle"}:
            continue
        if case.get("engine") != "GPU_DISK":
            continue
        # The material sweep uses the same geometry evidence directory and
        # intentionally creates many more periodic acceptance files. Those
        # rows are aggregated and validated by add_generic_material_scopes;
        # keep this matrix focused on the representative geometry/route
        # signatures so they do not inflate the expected 72 combinations.
        name = case.get("name", "")
        if any(marker in name for marker in (
            "-material-rates", "-material-sticking", "-bulk-", "-maskmat-",
            "-depositmat-", "-custom-id",
        )):
            continue
        comparisons = case.get("comparisons", {})
        if (case.get("status") != "verified" or case.get("engine") != "GPU_DISK"
                or case.get("productReturnCode") != 0 or case.get("directReturnCode") != 0
                or case.get("productActualEngine") != "GPU_DISK"
                or case.get("directActualEngine") != "GPU_DISK"
                or case.get("productVersions") != ENGINE_VERSIONS
                or case.get("directVersions") != ENGINE_VERSIONS
                or not comparisons or not all(value is True for value in comparisons.values())):
            raise SystemExit(f"periodic generic GPU evidence is incomplete: {case.get('name')}")
        project_path = ROOT / case["project"]
        direct_path = ROOT / case["directReference"]
        product_path = acceptance_path.parent / "worker-result.json"
        if not all(path.is_file() for path in (project_path, direct_path, product_path)):
            raise SystemExit(f"periodic generic evidence artifact is missing: {case.get('name')}")
        project = json.loads(project_path.read_text(encoding="utf-8"))
        if project.get("domain", {}).get("boundaryXY", project.get("domain", {}).get("xBoundary")) != "periodic":
            raise SystemExit(f"periodic generic case has a non-periodic project: {case.get('name')}")
        worker = json.loads(product_path.read_text(encoding="utf-8"))
        step = project["steps"][0]
        product_step = next((item for item in worker.get("stepRecords", [])
                             if item.get("stepId") == step["id"]), {})
        if worker.get("versions") != ENGINE_VERSIONS or product_step.get("actualEngine") != "GPU_DISK":
            raise SystemExit(f"periodic generic worker readback differs: {case.get('name')}")
        if case.get("geometry") == "fin":
            geometry_readback = case.get("productGeometryReadback", {})
            if (not geometry_readback or not case.get("directFinConstruction")
                    or not comparisons.get("finConstructionRoute")):
                raise SystemExit(f"periodic generic Fin route evidence is missing: {case.get('name')}")
            if (geometry_readback.get("requestedConstruction") == "native"
                    and (geometry_readback.get("automaticFallback")
                         or geometry_readback.get("automaticExtrusionFallback"))):
                automatic_fin_fallbacks.append({
                    "name": case["name"], "model": case["model"], "dimension": case["dimension"],
                    "operation": case["operation"], "finExtrusionMode": case.get("finExtrusionMode"),
                    "acceptance": str(acceptance_path.relative_to(ROOT)),
                    "productGeometryReadback": geometry_readback,
                    "comparisons": comparisons,
                })
        evidence = {
            "productWorker": str(product_path.relative_to(ROOT)),
            "productGuard": case["productGuard"],
            "directReference": str(direct_path.relative_to(ROOT)),
            "directGuard": case["directGuard"],
            "geometryAcceptance": str(acceptance_path.relative_to(ROOT)),
            "comparisons": comparisons, "versions": ENGINE_VERSIONS,
            "seed": product_step.get("actualSeed"), "actualEngine": product_step.get("actualEngine"),
        }
        row = gpu_row(project, step, case["model"], case["dimension"], evidence)
        if case.get("finExtrusionMode") is not None:
            row["finExtrusionMode"] = case["finExtrusionMode"]
        key = periodic_signature(row)
        if key in local_keys:
            continue
        local_keys.add(key)
        rows.append(row)
        accepted.append({
            "model": case["model"], "geometry": case["geometry"], "dimension": case["dimension"],
            "operation": case["operation"], "finExtrusionMode": case.get("finExtrusionMode"),
            "finConstruction": row.get("finConstruction"),
            "masked": "mask" in row["materialRoles"].get("Mask", []),
            "acceptance": str(acceptance_path.relative_to(ROOT)),
        })

    expected_count = 72
    if len(local_keys) != expected_count:
        raise SystemExit(f"periodic generic GPU signature matrix is incomplete: expected {expected_count}, got {len(local_keys)}")
    expected_fallbacks = {
        (model, operation, mode)
        for model in ("singleParticle", "multiParticle") for operation in ("etch", "deposit")
        for mode in ("direct", "extruded")
    }
    actual_fallbacks = {(item["model"], item["operation"], item["finExtrusionMode"])
                        for item in automatic_fin_fallbacks}
    if actual_fallbacks != expected_fallbacks:
        raise SystemExit(f"periodic masked Fin automatic fallback cases are incomplete: {sorted(expected_fallbacks - actual_fallbacks)}")
    report = json.loads(GEOMETRY_COVERAGE_REPORT.read_text(encoding="utf-8"))
    report["genericPeriodicGeometryCombinationsExpected"] = expected_count
    report["genericPeriodicGeometryCombinationsVerified"] = len(local_keys)
    report["genericPeriodicFinAutomaticFallbackCases"] = automatic_fin_fallbacks
    periodic_boolean_fin = sum(case["geometry"] == "fin" and case["finConstruction"] == "boolean"
                               for case in accepted)
    report["genericPeriodicBooleanFinCombinationsExpected"] = 24
    report["genericPeriodicBooleanFinCombinationsVerified"] = periodic_boolean_fin
    generic_boolean_fin = report.get("genericBooleanFinCombinationsVerified", 0)
    generic_masked_boolean_fin = report.get("genericMaskedBooleanFinCombinationsVerified", 0)
    report["totalBooleanFinCombinationsVerified"] = (
        report["specializedBooleanFinCombinationsVerified"] + generic_boolean_fin
        + generic_masked_boolean_fin + periodic_boolean_fin
    )
    failure_archive = GEOMETRY_PRESETS.parent / "known-failures-periodic-generic-fin"
    known_failures = []
    for failure_path in sorted(failure_archive.rglob("acceptance.json")):
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        known_failures.append({
            "name": failure.get("name"), "status": failure.get("status"),
            "model": failure.get("model"), "operation": failure.get("operation"),
            "finExtrusionMode": failure.get("finExtrusionMode"),
            "productReturnCode": failure.get("productReturnCode"),
            "directReturnCode": failure.get("directReturnCode"),
            "acceptance": str(failure_path.relative_to(ROOT)),
            "productLog": str((failure_path.parent / "product.log").relative_to(ROOT)),
            "directLog": str((failure_path.parent / "direct.log").relative_to(ROOT)),
            "productGuard": str((failure_path.parent / "product.guard.json").relative_to(ROOT)),
            "directGuard": str((failure_path.parent / "direct.guard.json").relative_to(ROOT)),
        })
    report["knownPeriodicGenericFinFailures"] = known_failures
    report["genericPeriodicFinDiagnosis"] = {
        "summary": "Periodic 3D masked generic Fin requests now use the independently verified Boolean construction. Extruded requests use a direct ViennaPS 3D box stencil over the requested y range. This route avoids non-finite coordinates from a redundant second surface extraction and product/direct final-mesh mismatches observed with the native Fin/Extrude path.",
        "knownFailureArchive": "docs/stage9-evidence/gpu-coverage-audit/geometry-presets/known-failures-periodic-generic-fin/",
        "knownFailureAttempts": len(known_failures),
        "verifiedAutomaticFallbackCases": len(automatic_fin_fallbacks),
    }
    GEOMETRY_COVERAGE_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"signatures": len(local_keys), "automaticFallbacks": len(automatic_fin_fallbacks)}


def validate_evidence_paths(rows: list[dict]) -> None:
    fields = ("productWorker", "productGuard", "directReference", "directGuard", "browserReport",
              "presetAcceptance", "geometryAcceptance", "multiIonProcessAcceptance", "singlePassFluxAcceptance",
              "processComparison", "replayComparison", "additionalSeedComparison", "nativeSplitProbeComparison",
              "nativeSplitProbeOutput", "nativeSplitProbeGuard", "additionalSeedGpuProbe", "additionalSeedGpuGuard",
              "productDataTransferManifest")
    for row in rows:
        for field in fields:
            reference = row.get("evidence", {}).get(field)
            if reference and not (ROOT / reference).exists():
                raise SystemExit(f"matrix evidence path does not resolve: {field}={reference}")


def add_gpu_triangle_rows(rows: list[dict]) -> None:
    """Publish the measured GPU_TRIANGLE product/direct pair only.

    The archived gpu-variants experiment ran the Stage 8 product worker and an
    independent direct ViennaPS SF6/O2 etch in 2D and 3D on the same stack.
    That is the entire measured GPU_TRIANGLE surface; no other model,
    operation, boundary or geometry is inferred from the GPU_DISK matrix.
    """
    expected_uuid = "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d"
    for dimension in (2, 3):
        case_dir = GPU_TRIANGLE_VARIANTS / f"gpu-triangle-{dimension}d"
        summary_path = case_dir / "summary-gpu_triangle.json"
        run_path = case_dir / f"sf6o2-{dimension}d-gpu_triangle.json"
        input_path = case_dir / f"sf6o2-{dimension}d-gpu_triangle.final.input.json"
        direct_path = case_dir / f"sf6o2-{dimension}d-gpu_triangle.final.direct-reference.json"
        product_guard = case_dir / f"sf6o2-{dimension}d-gpu_triangle.final.guard.json"
        direct_guard = case_dir / f"sf6o2-{dimension}d-gpu_triangle.final.direct.guard.json"
        for required in (summary_path, run_path, input_path, direct_path, product_guard, direct_guard):
            if not required.is_file():
                raise SystemExit(f"GPU_TRIANGLE evidence is missing: {required.relative_to(ROOT)}")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if not summary.get("passed") or len(summary.get("rows", [])) != 1:
            raise SystemExit(f"GPU_TRIANGLE {dimension}D summary is incomplete")
        row = summary["rows"][0]
        if (row.get("status") != "succeeded" or row.get("engine") != "GPU_TRIANGLE"
                or int(row.get("dimension", 0)) != dimension or row.get("model") != "sf6o2"):
            raise SystemExit(f"GPU_TRIANGLE {dimension}D case was not a successful measured run")
        actual = row.get("actualExecution", [])
        if len(actual) != 1 or actual[0].get("actualEngine") != "GPU_TRIANGLE":
            raise SystemExit(f"GPU_TRIANGLE {dimension}D product did not record its actual engine")
        native = actual[0].get("actualParameters", {}).get("nativeModel", {})
        if (native.get("actualEngine") != "GPU_TRIANGLE"
                or native.get("nativeProcessEngine") != "GPU_TRIANGLE"):
            raise SystemExit(f"GPU_TRIANGLE {dimension}D native model engine metadata differs")
        readback = actual[0].get("actualParameters", {}).get("cudaDeviceReadback", {})
        if (readback.get("readbackStatus") != "verified" or readback.get("uuid") != expected_uuid
                or not readback.get("process", {}).get("processListed")):
            raise SystemExit(f"GPU_TRIANGLE {dimension}D CUDA device readback is incomplete")
        comparisons = row.get("directReference", {}).get("comparisons", {})
        if set(comparisons) != {"initial", "final"} or not all(
                all(value is True for value in result.values()) for result in comparisons.values()):
            raise SystemExit(f"GPU_TRIANGLE {dimension}D product/direct comparison failed")
        for guard_path in (product_guard, direct_guard):
            guard = json.loads(guard_path.read_text(encoding="utf-8"))
            if guard.get("returnCode") != 0 or guard.get("timedOut") or guard.get("rssLimited"):
                raise SystemExit(f"GPU_TRIANGLE {dimension}D bounded child was not clean: {guard_path.name}")
        project = json.loads(input_path.read_text(encoding="utf-8"))
        step = project["steps"][0]
        evidence = {
            "productWorker": str(run_path.relative_to(ROOT)),
            "productGuard": str(product_guard.relative_to(ROOT)),
            "directReference": str(direct_path.relative_to(ROOT)),
            "directGuard": str(direct_guard.relative_to(ROOT)),
            "comparisons": comparisons,
            "versions": row["versions"], "seed": actual[0].get("actualSeed"),
            "actualEngine": "GPU_TRIANGLE", "gpuDeviceUuid": readback["uuid"],
        }
        gpu_row_value = gpu_row(project, step, "specialized:sf6o2", dimension, evidence)
        gpu_row_value["engine"] = "GPU_TRIANGLE"
        rows.append(gpu_row_value)


def main() -> int:
    specialized = json.loads(SPECIALIZED.read_text(encoding="utf-8"))
    multiparticle = json.loads(MULTIPARTICLE.read_text(encoding="utf-8"))
    if not specialized.get("passed") or not multiparticle.get("passed"):
        raise SystemExit("GPU matrix source has failed or incomplete cases")
    rows: list[dict] = []
    for case in specialized["rows"]:
        if case.get("status") != "succeeded" or case.get("engine") != "GPU_DISK":
            raise SystemExit(f"unverified GPU case: {case.get('model')}/{case.get('dimension')}")
        actual = case.get("actualExecution", [])
        if len(actual) != 1 or actual[0].get("actualEngine") != "GPU_DISK":
            raise SystemExit(f"product did not record GPU_DISK: {case.get('model')}/{case.get('dimension')}")
        native = actual[0].get("actualParameters", {}).get("nativeModel", {})
        if native.get("actualEngine") != "GPU_DISK" or native.get("nativeProcessEngine") != "GPU_DISK":
            raise SystemExit(f"native model engine metadata differs: {case.get('model')}/{case.get('dimension')}")
        compare = case.get("directReference", {}).get("comparisons", {})
        if not all(all(value is True for value in result.values()) for result in compare.values()) or set(compare) != {"initial", "final"}:
            raise SystemExit(f"product/direct GPU comparison failed: {case.get('model')}/{case.get('dimension')}")
        direct_path = ROOT / case["directReference"]["file"]
        input_project = json.loads(direct_path.with_name(direct_path.name.replace("direct-reference.json", "input.json")).read_text(encoding="utf-8"))
        step = input_project["steps"][0]
        rows.append(gpu_row(input_project, step, f"specialized:{case['model']}", case["dimension"], {
            "productWorker": case["artifactDirectory"],
            "productGuard": f"docs/stage9-evidence/gpu-specialized/{case['model']}-{case['dimension']}d-gpu_disk.final.guard.json",
            "directReference": case["directReference"]["file"],
            "directGuard": f"docs/stage9-evidence/gpu-specialized/{case['model']}-{case['dimension']}d-gpu_disk.final.direct.guard.json",
            "comparisons": compare, "versions": case["versions"], "seed": actual[0].get("actualSeed"),
            "cpuThreads": native.get("cpuThreads"), "actualEngine": native.get("actualEngine"),
        }))

    for case in multiparticle["rows"]:
        if case.get("status") != "succeeded" or case.get("engine") != "GPU_DISK":
            raise SystemExit(f"unverified MultiParticle GPU case: {case.get('dimension')}/{case.get('operation')}")
        compare = case.get("comparisons", {})
        if not compare or not all(value is True for value in compare.values()):
            raise SystemExit(f"MultiParticle product/direct GPU comparison failed: {case.get('dimension')}/{case.get('operation')}")
        input_project = json.loads((ROOT / case["inputProject"]).read_text(encoding="utf-8"))
        rows.append(gpu_row(input_project, input_project["steps"][0], "multiParticle", case["dimension"], {
            "productWorker": case["product"], "productGuard": case["guards"][0],
            "directReference": case["directReference"], "directGuard": case["guards"][1],
            "comparisons": compare, "versions": case["versions"],
            "seed": case["actualExecution"].get("actualSeed"), "actualEngine": "GPU_DISK",
        }))

    add_browser_multiparticle_rows(rows)
    add_browser_specialized_row(rows)
    add_singleparticle_and_neutral_rows(rows)
    add_default_ui_multiparticle_row(rows)
    add_multiparticle_split_flux_rows(rows)
    historical_gpu_profile_diagnostics = add_multiparticle_split_geometry_rows(rows)
    add_geometry_preset_rows(rows)
    add_gds_geometry_rows(rows)
    add_specialized_geometry_rows(rows)
    gds_specialized_failures = add_specialized_gds_rows(rows)
    add_generic_boolean_fin_rows(rows)
    add_gpu_triangle_rows(rows)
    add_generic_masked_pattern_rows(rows)
    add_periodic_generic_geometry_rows(rows)
    material_scope_report = add_generic_material_scopes(rows)
    specialized_material_scope_report = add_specialized_substrate_scope(rows)
    keys = set()
    for row in rows:
        key = tuple(row.get(name) for name in ("dimension", "model", "operation", "boundaryXY", "engine")) + (
            tuple(row["geometryKinds"]), tuple(row.get("patternKinds", [])), tuple(row["materials"]),
            json.dumps(row["materialRoles"], sort_keys=True), json.dumps(row["materialBindings"], sort_keys=True),
            json.dumps(row.get("speciesSignature"), sort_keys=True), row.get("finExtrusionMode"),
            row.get("finConstruction"), json.dumps(row.get("solverProfile"), sort_keys=True))
        if key in keys:
            raise SystemExit(f"duplicate GPU evidence signature: {key}")
        keys.add(key)

    validate_evidence_paths(rows)
    patched_gpu_multiion = verify_patched_gpu_multiion()
    result = {"schemaVersion": 2, "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "device": "NVIDIA GeForce RTX 3060 Laptop GPU",
              "sources": [str(SPECIALIZED.relative_to(ROOT)), str(MULTIPARTICLE.relative_to(ROOT)),
                          str(MULTIPARTICLE_SPLIT_FLUX_SUMMARY.relative_to(ROOT)),
                          str(MULTIPARTICLE_SPLIT_GEOMETRY_SUMMARY.relative_to(ROOT)),
                          str(BROWSER_MULTI.relative_to(ROOT)), str(BROWSER_SF6.relative_to(ROOT)),
                          str(SINGLEPARTICLE.relative_to(ROOT)), str(NEUTRAL_MULTIPARTICLE.relative_to(ROOT)),
            str(DEFAULT_UI_MULTIPARTICLE.relative_to(ROOT)), str(GEOMETRY_PRESETS.relative_to(ROOT)),
            str(GEOMETRY_COVERAGE_REPORT.relative_to(ROOT)),
            str(GDS_GEOMETRY.relative_to(ROOT)),
            str(GDS_MATERIAL_SCOPE.relative_to(ROOT)),
            str(GDS_MULTILAYER.relative_to(ROOT)),
            str(GDS_LAYER_COUNT.relative_to(ROOT)),
            str(GDS_SPECIALIZED.relative_to(ROOT)),
            str(GDS_SPECIALIZED_SINGLE.relative_to(ROOT)),
            str(GDS_SPECIALIZED_OFFSET.relative_to(ROOT)),
            str(GDS_SPECIALIZED_MASK.relative_to(ROOT)),
            str(GDS_SPECIALIZED_CPU.relative_to(ROOT)),
            str(GDS_SPECIALIZED_CPU_MATRIX.relative_to(ROOT)),
            str(GDS_SPECIALIZED_BROWSER.relative_to(ROOT)),
            str(GDS_NATIVE_FAILURE_BROWSER.relative_to(ROOT)),
            str(GDS_SPECIALIZED_GDB.relative_to(ROOT)),
            *patched_gpu_multiion["evidence"],
            *[str((MATERIAL_SCOPE / f"summary-{name}-gpu_disk.json").relative_to(ROOT))
              for name in ("builtins-fin", "holes", "fins", "custom")],
            str((MATERIAL_SCOPE / "summary-all-fin-periodic-gpu_disk.json").relative_to(ROOT)),
            str(MATERIAL_SCOPE_REPORT.relative_to(ROOT)),
            str((SPECIALIZED_MATERIAL_SCOPE / "selectable-substrates-summary.json").relative_to(ROOT)),
            str((SPECIALIZED_MATERIAL_SCOPE / "specialized-substrate-capability-report.json").relative_to(ROOT))],
              "materialScopeRows": material_scope_report["scopedCapabilityRowCount"]
              + sum(bool(row.get("materialScope")) and row.get("model", "").startswith("specialized:") for row in rows)
              + sum(bool(row.get("materialScope")) and row.get("geometryKinds") == ["gds"] for row in rows),
              "genericMaterialScopeRows": material_scope_report["scopedCapabilityRowCount"],
              "specializedMaterialScopeRows": sum(bool(row.get("materialScope")) and row.get("model", "").startswith("specialized:") for row in rows),
              "gdsMaterialScopeRows": sum(bool(row.get("materialScope")) and row.get("geometryKinds") == ["gds"] for row in rows),
              "noFallback": True, "knownNativeFailures": gds_specialized_failures,
              # Older failures belong to the symmetric multi-ion splitting
              # route. The repaired native route has independent direct GPU
              # matches for every listed solver profile, so these reports are
              # retained as history and no longer suppress UI coverage.
              "knownGpuProfileFailures": [],
              "historicalSplitRouteGpuProfileDiagnostics": [
                  {**row,
                   "historicalExecutionStrategy": "symmetric-operator-splitting-v1",
                   "status": "historical-route-mismatch",
                   "resolvedForExecutionStrategy": "native-multi-ion-v1",
                   "resolutionEvidence": str(MULTIPARTICLE_PATCHED_PROFILE_RECHECK.relative_to(ROOT))}
                  for row in historical_gpu_profile_diagnostics],
              # The old stock-wheel single-ion diagnostic remains in the
              # evidence archive, but it does not describe the patched
              # runtime used by the accepted product and must not gate GPU.
              "knownEngineLimitations": [], "resolvedGpuMultiIonSupport": patched_gpu_multiion,
              "historicalStockGpuMultiIonDiagnostics": [
                  str(MULTIPARTICLE_GPU_SPECIES.relative_to(ROOT)),
                  str(MULTIPARTICLE_GPU_WARNING.relative_to(ROOT)),
                  str(MULTIPARTICLE_CPU_ION_MATRIX.relative_to(ROOT)),
                  str(MULTIPARTICLE_LIMIT_BROWSER.relative_to(ROOT)),
                  str(MULTIPARTICLE_GPU_FLUX.relative_to(ROOT)),
                  str(MULTIPARTICLE_CPU_FLUX.relative_to(ROOT)),
                  str(MULTIPARTICLE_GPU_CLASS_ROUTE.relative_to(ROOT)),
                  str(MULTIPARTICLE_GPU_CLASS_ROUTE_GUARD.relative_to(ROOT)),
                  str(MULTIPARTICLE_NATIVE_API.relative_to(ROOT)),
                  str(MULTIPARTICLE_NATIVE_API_GUARD.relative_to(ROOT)),
                  str(MULTIPARTICLE_VELOCITY_API.relative_to(ROOT)),
                  str(MULTIPARTICLE_VELOCITY_API_GUARD.relative_to(ROOT)),
                  str(MULTIPARTICLE_VELOCITY_ASSIGNMENT.relative_to(ROOT)),
                  str(MULTIPARTICLE_VELOCITY_ASSIGNMENT_GUARD.relative_to(ROOT)),
              ],
              "rows": rows}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "verified", "rows": len(rows), "matrix": str(OUTPUT.relative_to(ROOT))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
