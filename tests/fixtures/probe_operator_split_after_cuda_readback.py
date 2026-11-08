"""Check whether the pre-run CUDA UUID synchronization affects GPU ray setup."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.gpu_devices import cuda_devices, resolve_gpu_device


def main(project_path: Path, output_path: Path) -> None:
    inventory = cuda_devices(refresh=True)
    device = resolve_gpu_device(None, inventory)
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    os.environ["CUDA_VISIBLE_DEVICES"] = device["uuid"]
    import viennaps as ps
    from worker.simulate import cuda_runtime_readback
    sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
    from probe_multiparticle_gpu_operator_split import run_split

    project = json.loads(project_path.read_text(encoding="utf-8"))
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    ps.setDimension(int(project["dimension"]))
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    readback = cuda_runtime_readback(device)
    result = run_split(project, 1)
    output_path.write_text(json.dumps({"device": device, "preReadback": readback,
                                       "elapsedSeconds": result["totalElapsedSeconds"],
                                       "callbacksBySpecies": result["callbacksBySpecies"],
                                       "final": result["final"]}, ensure_ascii=False,
                                      allow_nan=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "succeeded", "device": device,
                      "preReadback": readback, "output": str(output_path)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: probe_operator_split_after_cuda_readback.py PROJECT OUTPUT")
    main(Path(sys.argv[1]), Path(sys.argv[2]))
