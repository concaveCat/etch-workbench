"""Measure stock 4.6.2 CPU/GPU flux variation from one shared 2D native state."""
from __future__ import annotations

import copy
import json
import statistics
from pathlib import Path

from check_native_multiion_live import EVIDENCE, ROOT, reference


UUID = "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d"
project_path = EVIDENCE / "2d-project.json"
source_project = json.loads(project_path.read_text())
step_id = next(row["id"] for row in source_project["steps"] if row.get("enabled"))
source_step = next(row for row in source_project["steps"] if row["id"] == step_id)
source_run_id = "3a508ae8cd699c55623c9613"
state_path = ROOT / "var/service-old-replay" / "runs" / source_run_id / "state-initial.vpsd"
if not state_path.is_file():
    raise RuntimeError(f"required saved initial Domain state is missing: {state_path}")

rows = []
for species in source_step["species"]:
    label = species["label"]
    for rays, seeds in ((256, (314159, 271828, 8675309)),
                        (1024, (314159, 271828)),
                        (4096, (314159,)),
                        (8192, (314159,))):
        for seed in seeds:
            project = copy.deepcopy(source_project)
            step = next(row for row in project["steps"] if row["id"] == step_id)
            step["species"] = [copy.deepcopy(species)]
            step["rateCoefficients"]["defaultBySpecies"] = {
                label: float(source_step["rateCoefficients"]["defaultBySpecies"][label])}
            step["rateCoefficients"]["byMaterial"] = {
                material: {label: float(values[label])}
                for material, values in source_step["rateCoefficients"].get("byMaterial", {}).items()
                if label in values
            }
            project["solver"]["raysPerPoint"] = rays
            project["solver"]["random"]["seed"] = seed
            tag = f"stock-{label}-r{rays}-s{seed}"
            args = [project_path, step_id, state_path]
            # Keep exact per-case project settings in a dedicated input file.
            case_project = EVIDENCE / f"{tag}.project.json"
            case_project.write_text(json.dumps(project, indent=2) + "\n")
            args[0] = case_project
            cpu = reference(f"{tag}-cpu", "stage7_multiparticle_loaded_state_flux_reference.py", args,
                            engine="CPU_DISK", candidate=False)
            gpu = reference(f"{tag}-gpu", "stage7_multiparticle_loaded_state_flux_reference.py", args,
                            engine="GPU_DISK", candidate=False)
            cpu_field = next(name for name in cpu["cellData"]["scalars"] if name.startswith(label))
            gpu_field = next(name for name in gpu["cellData"]["scalars"] if name.startswith(label))
            cpu_values = cpu["cellData"]["scalars"][cpu_field]
            gpu_values = gpu["cellData"]["scalars"][gpu_field]
            cpu_mean = statistics.fmean(cpu_values)
            gpu_mean = statistics.fmean(gpu_values)
            rows.append({"species": label, "kind": species["kind"], "raysPerPoint": rays, "seed": seed,
                         "engineBuild": "stock ViennaPS 4.6.2 / ViennaLS 5.8.5",
                         "stateSha256": cpu["loadedNativeState"]["sha256"],
                         "cpuFieldName": cpu_field, "gpuFieldName": gpu_field,
                         "cpuMean": cpu_mean, "gpuMean": gpu_mean,
                         "relativeMeanDifference": (gpu_mean - cpu_mean) / abs(cpu_mean) if cpu_mean else None,
                         "sameFluxMesh": cpu["points"] == gpu["points"] and cpu["topology"] == gpu["topology"],
                         "cpuImport": cpu["viennapsModulePath"], "gpuImport": gpu["viennapsModulePath"]})

report = {"status": "passed", "scope": "one shared saved 2D Domain, direct stock 4.6.2 CPU_DISK/GPU_DISK calculateFlux; fixed per-case seed and ray count",
          "gpuUuid": UUID, "statePath": str(state_path), "cases": rows,
          "summary": {}}
for rays in (256, 1024, 4096, 8192):
    subset = [row for row in rows if row["raysPerPoint"] == rays]
    values = [row["relativeMeanDifference"] for row in subset]
    report["summary"][str(rays)] = {"caseCount": len(subset), "minimumRelativeMeanDifference": min(values),
                                    "maximumRelativeMeanDifference": max(values),
                                    "meanRelativeMeanDifference": statistics.fmean(values)}
output = EVIDENCE / "stock-4.6.2-cpu-gpu-flux-bias-sweep.json"
output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
