"""Compare a completed Workbench result with its direct native GPU reference."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product"


def product_mesh(snapshot: dict) -> dict:
    topology = snapshot["topology"]
    cells = snapshot["lines"] if topology == "lines" else snapshot["triangles"]
    return {
        "points": snapshot["points"],
        "cells": cells,
        "materialIds": snapshot["cellData"]["materialIds"],
        "materialIdsByLevelSetIndex": snapshot["materialIdsByLevelSetIndex"],
    }


def native_mesh(snapshot: dict) -> dict:
    return {key: snapshot[key] for key in (
        "points", "cells", "materialIds", "materialIdsByLevelSetIndex")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--device-uuid", required=True)
    parser.add_argument("--direct-output", type=Path, default=EVIDENCE / "eight-ion-2d-gpu_disk.json")
    args = parser.parse_args()
    run_dir = args.data_dir.resolve(strict=True) / "runs" / args.run_id
    worker = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    direct = json.loads(args.direct_output.resolve(strict=True).read_text(encoding="utf-8"))
    if manifest["status"] != "succeeded" or direct["status"] != "succeeded":
        raise SystemExit("the browser run or direct ViennaPS reference did not succeed")
    if direct["actualEngine"] != "GPU_DISK":
        raise SystemExit("direct reference did not execute GPU_DISK")
    if not Path(direct["viennapsModulePath"]).resolve().is_relative_to(args.runtime_dir.resolve(strict=True)):
        raise SystemExit("direct reference did not load the patched candidate runtime")
    if worker["cudaDeviceReadback"]["uuid"] != args.device_uuid:
        raise SystemExit("Workbench did not report the expected compute GPU UUID")
    if worker["actualSteps"][0]["engine"] != "GPU_DISK":
        raise SystemExit("Workbench did not run the step on GPU_DISK")

    snapshots = {row["stepId"]: row for row in worker["snapshots"]}
    initial_meta = snapshots["initial"]
    final_meta = next(row for key, row in snapshots.items() if key != "initial")
    dimension = int(final_meta["outputs"][0]["dimension"])
    initial_product = json.loads((run_dir / initial_meta["snapshot"]).read_text(encoding="utf-8"))
    final_product = json.loads((run_dir / final_meta["snapshot"]).read_text(encoding="utf-8"))
    pairs = {
        "initial": (native_mesh(direct["initial"]), product_mesh(initial_product)),
        "final": (native_mesh(direct["final"]), product_mesh(final_product)),
    }
    comparisons = {}
    for label, (reference, result) in pairs.items():
        comparisons[label] = {key: reference[key] == result[key] for key in reference}
    report = {
        "status": "passed" if all(all(row.values()) for row in comparisons.values()) else "mismatch",
        "runId": args.run_id,
        "dimension": dimension,
        "engine": worker["actualSteps"][0]["engine"],
        "gpuUuid": worker["cudaDeviceReadback"]["uuid"],
        "directRequestedGpuUuid": args.device_uuid,
        "runtimeFingerprint": worker["nativeEngineFingerprint"],
        "directViennaPSModulePath": direct["viennapsModulePath"],
        "speciesOrder": direct["actualParameters"]["speciesOrder"],
        "comparisons": comparisons,
        "pointCount": {"direct": len(direct["final"]["points"]),
                       "workbench": len(final_product["points"])},
        "cellCount": {"direct": len(direct["final"]["cells"]),
                      "workbench": len(product_mesh(final_product)["cells"])},
    }
    output = EVIDENCE / f"eight-ion-ui-direct-{dimension}d-comparison.json"
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
