"""Run the production split helper on the independent direct-reference Domain."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.gpu_devices import cuda_devices, resolve_gpu_device


def main(project_path: Path, run_dir: Path, output_path: Path) -> None:
    project = json.loads(project_path.read_text(encoding="utf-8"))
    inventory = cuda_devices(refresh=True)
    device = resolve_gpu_device(project["solver"].get("gpuDeviceId"), inventory)
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    os.environ["CUDA_VISIBLE_DEVICES"] = device["uuid"]
    import viennaps as ps
    from worker.simulate import (apply_gpu_multi_particle_split, configure_native_materials,
                                 cuda_runtime_readback)
    sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
    from gpu_geometry_direct_reference import make_domain as direct_make_domain, snapshot

    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    ps.setDimension(int(project["dimension"]))
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    configure_native_materials(ps, project.get("materialDefinitions", []))
    domain, geometry_readback = direct_make_domain(
        project, scratch=run_dir / "direct-geometry-scratch")
    step = next(row for row in project["steps"] if row.get("enabled"))
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(ps.MaterialRegistry.instance().getMaterial(step["depositMaterial"]))
    initial = snapshot(domain, project["dimension"])
    cuda_runtime_readback(device)
    intermediate = run_dir / "intermediate" / step["id"]
    intermediate.mkdir(parents=True, exist_ok=True)
    result = apply_gpu_multi_particle_split(ps, domain, project, step, seed=int(project["solver"]["random"]["seed"]),
                                            run_dir=run_dir, intermediate_dir=intermediate,
                                            selected_gpu_device=device)
    payload = {"status": "succeeded", "engine": "GPU_DISK", "device": device,
               "geometryReadback": geometry_readback,
               "initial": initial,
               "execution": result["execution"], "callbacks": result["callbackState"],
               "final": snapshot(domain, project["dimension"])}
    output_path.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({"status": payload["status"], "device": device,
                      "stages": len(result["execution"]["stages"]), "output": str(output_path)},
                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: probe_worker_multiparticle_split_helper.py PROJECT RUN_DIR OUTPUT")
    main(*(Path(value) for value in sys.argv[1:]))
