"""Discover CUDA devices through the bounded-child process guard."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import storage


ROOT = storage.ROOT
GUARD = ROOT / "scripts" / "bounded_child.py"
PROBE = ROOT / "worker" / "enumerate_cuda_devices.py"
PYTHON = ROOT / ".venv" / "bin" / "python"
_LOCK = threading.Lock()
_CACHE: tuple[float, dict[str, Any]] | None = None
_CACHE_SECONDS = 5.0


def cuda_devices(*, refresh: bool = False) -> dict[str, Any]:
    global _CACHE
    with _LOCK:
        if not refresh and _CACHE and time.monotonic() - _CACHE[0] < _CACHE_SECONDS:
            return json.loads(json.dumps(_CACHE[1]))
        evidence = storage.VAR / "gpu-device-selection"
        evidence.mkdir(parents=True, exist_ok=True)
        stem = f"inventory-{time.time_ns()}"
        record = evidence / f"{stem}.guard.json"
        log = evidence / f"{stem}.log"
        command = [str(PYTHON), str(GUARD), "--cwd", str(ROOT), "--record", str(record),
                   "--log-path", str(log), "--timeout-seconds", "12", "--max-rss-mib", "512",
                   "--max-log-bytes", "65536", "--", str(PYTHON), str(PROBE)]
        try:
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                       timeout=15, check=False)
            guard = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else {}
            payload = json.loads(log.read_text(encoding="utf-8")) if log.is_file() else {}
            if (completed.returncode != 0 or guard.get("returnCode") != 0
                    or guard.get("remainingOwnedGroupMembers")):
                raise RuntimeError("bounded device probe failed or did not reap all child processes")
            devices = payload.get("devices")
            if payload.get("status") not in {"available", "unavailable"} or not isinstance(devices, list):
                raise RuntimeError("bounded device probe returned an invalid inventory")
            response = {"status": payload["status"], "devices": devices,
                        "preferredGpuDeviceId": payload.get("preferredGpuDeviceId"),
                        "reason": payload.get("reason"),
                        "probe": {"bounded": True, "returnCode": guard.get("returnCode"),
                                  "elapsedSeconds": guard.get("elapsedSeconds")}}
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            response = {"status": "error", "devices": [], "preferredGpuDeviceId": None,
                        "reason": f"{type(exc).__name__}: {exc}", "probe": {"bounded": True}}
        _CACHE = (time.monotonic(), response)
        return json.loads(json.dumps(response))


def resolve_gpu_device(selection: str | None, inventory: dict[str, Any]) -> dict[str, Any]:
    devices = inventory.get("devices", [])
    if selection is None:
        wanted = inventory.get("preferredGpuDeviceId")
    else:
        wanted = selection
    match = next((device for device in devices if device.get("uuid") == wanted), None)
    if match is None:
        if selection is None:
            raise RuntimeError("no CUDA-capable NVIDIA GPU is available for the selected GPU engine")
        raise RuntimeError(f"selected compute GPU {selection} is not available on this host")
    return dict(match)
