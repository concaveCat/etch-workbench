"""Validate and index the accepted multi-ion GPU process/flux evidence."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe"
OPERATOR = BASE / "operator-split"
FLUX = OPERATOR / "single-pass-flux-species"
BROWSER_REPORT = FLUX / "browser-acceptance.json"
RTX_UUID = "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d"
VERSIONS = {"viennaps": "4.6.2", "viennals": "5.8.5"}
CASES = [
    {
        "dimension": 2,
        "project": OPERATOR / "stack-trench-2d-multi-ion-etch.project.json",
        "processAcceptance": BASE / "product-worker/stack-trench-2d-multi-ion-etch/after-readback-comparison.json",
        "processWorker": BASE / "product-worker/stack-trench-2d-multi-ion-etch/after-readback/worker-result.json",
        "processGuard": BASE / "product-worker/stack-trench-2d-multi-ion-etch/after-readback/child-record.json",
        "processDirect": OPERATOR / "stack-trench-2d-multi-ion-etch-cpu.json",
        "processDirectGuard": OPERATOR / "stack-trench-2d-multi-ion-etch-cpu.guard.json",
        "fluxAcceptance": FLUX / "product-worker/flux-comparison.json",
    },
    {
        "dimension": 3,
        "project": OPERATOR / "stack-trench-3d-multi-ion-deposit.project.json",
        "processAcceptance": BASE / "product-worker/stack-trench-3d-multi-ion-deposit/after-readback-comparison.json",
        "processWorker": BASE / "product-worker/stack-trench-3d-multi-ion-deposit/after-readback/worker-result.json",
        "processGuard": BASE / "product-worker/stack-trench-3d-multi-ion-deposit/after-readback/child-record.json",
        "processDirect": OPERATOR / "stack-trench-3d-multi-ion-deposit-cpu.json",
        "processDirectGuard": OPERATOR / "stack-trench-3d-multi-ion-deposit-cpu.guard.json",
        "fluxAcceptance": FLUX / "3d-stack-deposit/flux-comparison.json",
    },
]


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"missing evidence file: {path.relative_to(ROOT)}")
    return str(path.relative_to(ROOT))


def guard_ok(path: Path) -> bool:
    row = read(path)
    return (row.get("returnCode") == 0 and row.get("timedOut") is False
            and row.get("rssLimited") is False and row.get("logLimited") is False
            and row.get("remainingOwnedGroupMembers") == [])


def all_checks(report: dict[str, Any], label: str) -> None:
    checks = report.get("checks")
    if report.get("status") != "passed" or not checks or not all(value is True for value in checks.values()):
        raise SystemExit(f"failed or incomplete {label}: {checks}")


def validate_case(case: dict[str, Any], browser_case: dict[str, Any]) -> dict[str, Any]:
    dimension = case["dimension"]
    project = read(case["project"])
    process = read(case["processAcceptance"])
    flux = read(case["fluxAcceptance"])
    process_worker = read(case["processWorker"])
    process_guard = read(case["processGuard"])
    process_direct_guard = read(case["processDirectGuard"])
    all_checks(process, f"{dimension}D multi-ion process comparison")
    all_checks(flux, f"{dimension}D single-pass flux comparison")
    if (process.get("device", {}).get("uuid") != RTX_UUID
            or process_worker.get("versions") != VERSIONS):
        raise SystemExit(f"{dimension}D process version/device does not match this acceptance")
    if flux.get("device", {}).get("uuid") != RTX_UUID:
        raise SystemExit(f"{dimension}D flux was not executed on the selected RTX 3060")
    if process_worker.get("versions") != VERSIONS or process_worker.get("selectedGpuDevice", {}).get("uuid") != RTX_UUID:
        raise SystemExit(f"{dimension}D process worker version/device does not match")
    process_actual = process_worker.get("actualSteps", [])
    if (len(process_actual) != 1 or process_actual[0].get("engine") != "GPU_DISK"
            or process_actual[0].get("executionStrategy") != "symmetric-operator-splitting-v1"):
        raise SystemExit(f"{dimension}D process worker did not record the verified split strategy")
    process_guard_ok = guard_ok(case["processGuard"]) and guard_ok(case["processDirectGuard"])
    flux_guards = flux.get("guards", {})
    flux_guard_ok = (bool(flux_guards.get("cpu")) and bool(flux_guards.get("gpu"))
                     and guard_ok(ROOT / flux_guards["cpu"]) and guard_ok(ROOT / flux_guards["gpu"]))
    if not process_guard_ok or not flux_guard_ok:
        raise SystemExit(f"{dimension}D bounded process/direct/flux child did not exit cleanly")

    flux_worker_path = ROOT / flux["artifacts"]["productWorker"]
    flux_worker = read(flux_worker_path)
    flux_payload = read(ROOT / flux["artifacts"]["productFlux"])
    flux_actual = flux_worker.get("actualSteps", [])
    strategy = flux_payload.get("execution", {}).get("multiParticleFlux", {})
    if (flux_worker.get("versions") != VERSIONS
            or flux_worker.get("selectedGpuDevice", {}).get("uuid") != RTX_UUID
            or len(flux_actual) != 1 or flux_actual[0].get("engine") != "GPU_DISK"
            or flux_payload.get("execution", {}).get("actualEngine") != "GPU_DISK"
            or strategy.get("strategy") != "per-species-native-mesh-merge-v1"):
        raise SystemExit(f"{dimension}D product flux worker did not record native GPU strategy/device")

    if browser_case.get("dimension") != dimension or browser_case.get("actualEngine") != "GPU_DISK":
        raise SystemExit(f"browser UI did not run the {dimension}D project on GPU_DISK")
    if browser_case.get("selectedGpuDevice", {}).get("uuid") != RTX_UUID:
        raise SystemExit(f"browser UI selected the wrong GPU for {dimension}D")
    if browser_case.get("processStrategy") != "symmetric-operator-splitting-v1":
        raise SystemExit(f"browser UI did not execute the {dimension}D multi-ion process strategy")
    process_browser_comparison = read(ROOT / browser_case["processComparison"])
    process_replay_comparison = read(ROOT / browser_case["processReplayComparison"])
    all_checks(process_browser_comparison, f"{dimension}D browser process direct comparison")
    all_checks(process_replay_comparison, f"{dimension}D browser process replay direct comparison")
    process_browser_worker = read(ROOT / browser_case["processWorker"])
    process_replay_worker = read(ROOT / browser_case["processReplayWorker"])
    process_browser_guard = ROOT / browser_case["processGuard"]
    process_replay_guard = ROOT / browser_case["processReplayGuard"]
    if (not guard_ok(process_browser_guard) or not guard_ok(process_replay_guard)
            or process_browser_worker.get("selectedGpuDevice", {}).get("uuid") != RTX_UUID
            or process_replay_worker.get("selectedGpuDevice", {}).get("uuid") != RTX_UUID
            or process_browser_worker.get("actualSteps", [{}])[0].get("executionStrategy") != "symmetric-operator-splitting-v1"
            or process_replay_worker.get("actualSteps", [{}])[0].get("executionStrategy") != "symmetric-operator-splitting-v1"):
        raise SystemExit(f"browser {dimension}D process/replay did not preserve GPU strategy, UUID, and clean child guards")
    process_replay = read(ROOT / browser_case["processReplayConfig"])
    if (process_replay.get("analysisMode") != "process"
            or process_replay.get("replayMode") != "from-initial"
            or process_replay.get("executionRequirements", {}).get("steps", [{}])[0].get("executionStrategy")
            != "symmetric-operator-splitting-v1"):
        raise SystemExit(f"browser {dimension}D process replay did not preserve its strategy and source mode")
    if len(browser_case.get("fieldComparisons", [])) != 3:
        raise SystemExit(f"browser UI did not verify every {dimension}D species field")
    if browser_case.get("historyReloaded") is not True:
        raise SystemExit(f"browser history did not reload the {dimension}D flux result")

    browser_product_guard = ROOT / browser_case["productGuard"]
    browser_replay_guard = ROOT / browser_case["replayGuard"]
    browser_product_worker = read(ROOT / browser_case["productWorker"])
    browser_replay_worker = read(ROOT / browser_case["replayWorker"])
    if (not guard_ok(browser_product_guard) or not guard_ok(browser_replay_guard)
            or browser_product_worker.get("selectedGpuDevice", {}).get("uuid") != RTX_UUID
            or browser_replay_worker.get("selectedGpuDevice", {}).get("uuid") != RTX_UUID
            or browser_replay_worker.get("actualSteps", [{}])[0].get("fluxStrategy") != "per-species-native-mesh-merge-v1"):
        raise SystemExit(f"browser {dimension}D run/replay bounded worker evidence is incomplete")
    browser_flux = read(ROOT / browser_case["fluxJson"])
    replay_flux = read(ROOT / browser_case["replayFluxJson"])
    if any(browser_flux.get(field) != replay_flux.get(field) for field in ("points", "topology", "cellData")):
        raise SystemExit(f"browser {dimension}D replay output differs from its original flux result")
    for evidence_key in ("processInput", "processWorker", "processGuard", "processComparison",
                         "processReplayConfig", "processReplayWorker", "processReplayGuard", "processReplayComparison",
                         "productWorker", "productGuard", "fluxJson", "mergedVtp", "replayConfig",
                         "replayFluxJson", "replayWorker", "replayGuard", "runZip", "cpuDirectReference"):
        if not (ROOT / browser_case[evidence_key]).is_file():
            raise SystemExit(f"browser {dimension}D evidence is missing: {browser_case[evidence_key]}")
    for species in project["steps"][0]["species"]:
        csv_path = FLUX / f"browser-{dimension}d-{species['label']}00.csv"
        if not csv_path.is_file():
            raise SystemExit(f"browser {dimension}D field CSV is missing: {csv_path.relative_to(ROOT)}")

    case["project"] = rel(case["project"])
    for key in ("processAcceptance", "processWorker", "processGuard", "processDirect", "processDirectGuard", "fluxAcceptance"):
        case[key] = rel(case[key])
    case["processChildGuardsClean"] = process_guard_ok
    case["fluxChildGuardsClean"] = flux_guard_ok
    case["browserAndReplayChildrenClean"] = True
    case["browserRunId"] = browser_case["runId"]
    case["browserReplayRunId"] = browser_case["replayRunId"]
    case["browserProcessRunId"] = browser_case["processRunId"]
    case["browserProcessReplayRunId"] = browser_case["processReplayRunId"]
    case["browserProcessStrategy"] = browser_case["processStrategy"]
    case["browserProcessDirectComparison"] = browser_case["processComparison"]
    case["browserProcessReplayDirectComparison"] = browser_case["processReplayComparison"]
    case["browserProductWorker"] = browser_case["productWorker"]
    case["browserReplayWorker"] = browser_case["replayWorker"]
    case["browserProductGuard"] = browser_case["productGuard"]
    case["browserReplayGuard"] = browser_case["replayGuard"]
    case["browserReplayConfig"] = browser_case["replayConfig"]
    case["browserRunZip"] = browser_case["runZip"]
    return case


def main() -> None:
    browser = read(BROWSER_REPORT)
    if (browser.get("status") != "passed" or browser.get("installedVersions") != VERSIONS
            or {row.get("dimension") for row in browser.get("cases", [])} != {2, 3}
            or browser.get("selectedDevice", {}).get("uuid") != RTX_UUID or browser.get("pageErrors") != []):
        raise SystemExit("2D/3D browser GPU flux acceptance is incomplete")
    browser_by_dimension = {row["dimension"]: row for row in browser["cases"]}
    cases = [validate_case(case.copy(), browser_by_dimension[case["dimension"]]) for case in CASES]
    convergence = [
        FLUX / "high-rays/flux-comparison.json",
        FLUX / "3d-stack-deposit/high-rays/flux-comparison.json",
    ]
    for path in convergence:
        report = read(path)
        all_checks(report, "1024-rays sampling-convergence report")
        if report.get("device", {}).get("uuid") != RTX_UUID:
            raise SystemExit(f"sampling-convergence report did not execute on selected RTX: {path}")

    result = {
        "schemaVersion": 1,
        "status": "passed",
        "scope": "real ViennaPS 4.6.2 MultiParticle process and single-pass calculateFlux through the 8799 UI/API, with direct CPU references, native VTP field checks, process/flux replay, save/open, and history reload",
        "versions": VERSIONS,
        "device": {"name": "NVIDIA GeForce RTX 3060 Laptop GPU", "uuid": RTX_UUID},
        "processStrategy": "symmetric-operator-splitting-v1",
        "fluxStrategy": "per-species-native-mesh-merge-v1",
        "cases": cases,
        "samplingConvergence": [rel(path) for path in convergence],
        "browserAcceptance": {"status": "passed", "report": rel(BROWSER_REPORT),
                              "dimensions": [2, 3], "service": browser["service"],
                              "isolatedDataDir": str(Path(browser["isolatedDataDir"]).relative_to(ROOT))},
        "checks": {
            "versionsPinned": True,
            "nativeProcessGpuAndDirectCpuComparisons": True,
            "nativeFluxMeshAndFieldAssociationComparisons": True,
            "allBoundedChildrenClean": True,
            "chromiumInputOutputProcessAndFluxReplaySaveOpenHistory": True,
            "twoDimensionalAndThreeDimensional": True,
            "raySamplingConvergence": True,
        },
    }
    output = FLUX / "summary.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "cases": len(cases),
                      "browserCases": len(browser["cases"]), "output": str(output.relative_to(ROOT))},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
