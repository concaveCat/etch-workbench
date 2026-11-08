"""Read-only inventory of NVIDIA CUDA GPUs visible to the local system."""
from __future__ import annotations

import csv
import io
import json
import re
import subprocess


def main() -> None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,pci.bus_id,uuid,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "unavailable", "devices": [],
                          "reason": f"{type(exc).__name__}: {exc}"}))
        return
    if result.returncode != 0:
        print(json.dumps({"status": "unavailable", "devices": [],
                          "reason": (result.stderr or "nvidia-smi failed").strip()[:1000]}))
        return
    devices = []
    for row in csv.reader(io.StringIO(result.stdout), skipinitialspace=True):
        if len(row) != 5:
            continue
        index, name, bus_id, uuid, memory = [value.strip() for value in row]
        if not re.fullmatch(r"\d+", index) or not uuid.startswith("GPU-"):
            continue
        try:
            memory_mib = int(memory)
        except ValueError:
            memory_mib = None
        devices.append({"index": int(index), "name": name, "pciBusId": bus_id,
                        "uuid": uuid, "memoryTotalMiB": memory_mib,
                        "vendor": "NVIDIA", "computeApi": "CUDA"})
    devices.sort(key=lambda item: item["index"])
    preferred = max(devices, key=lambda item: (item["memoryTotalMiB"] or 0, -item["index"]), default=None)
    print(json.dumps({"status": "available" if devices else "unavailable", "devices": devices,
                      "preferredGpuDeviceId": preferred["uuid"] if preferred else None,
                      "reason": None if devices else "nvidia-smi reported no CUDA-capable NVIDIA GPU"},
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
