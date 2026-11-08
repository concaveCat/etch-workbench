#!/usr/bin/env python3
"""Bounded, read-only inventory of the installed ViennaPS/CUDA device API."""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import subprocess
from importlib.metadata import version
from pathlib import Path


def load_driver():
    name = ctypes.util.find_library("cuda")
    if not name:
        return None, {"library": None}
    lib = ctypes.CDLL(name)
    result: dict[str, object] = {"library": name}

    def call(function: str, *args):
        fn = getattr(lib, function)
        fn.restype = ctypes.c_int
        return fn(*args)

    init = call("cuInit", 0)
    count = ctypes.c_int()
    count_result = call("cuDeviceGetCount", ctypes.byref(count)) if init == 0 else init
    devices = []
    if count_result == 0:
        for ordinal in range(count.value):
            device = ctypes.c_int()
            if call("cuDeviceGet", ctypes.byref(device), ordinal) != 0:
                continue
            name_buf = ctypes.create_string_buffer(256)
            bus_buf = ctypes.create_string_buffer(32)
            uuid_buf = (ctypes.c_ubyte * 16)()
            name_result = call("cuDeviceGetName", name_buf, len(name_buf), device)
            bus_result = call("cuDeviceGetPCIBusId", bus_buf, len(bus_buf), device)
            uuid_fn = getattr(lib, "cuDeviceGetUuid_v2", None) or getattr(lib, "cuDeviceGetUuid", None)
            uuid_result = None
            if uuid_fn:
                uuid_fn.restype = ctypes.c_int
                uuid_result = uuid_fn(ctypes.byref(uuid_buf), device)
            uuid_hex = bytes(uuid_buf).hex()
            uuid = f"GPU-{uuid_hex[:8]}-{uuid_hex[8:12]}-{uuid_hex[12:16]}-{uuid_hex[16:20]}-{uuid_hex[20:]}" if uuid_result == 0 else None
            devices.append({"ordinal": ordinal, "name": name_buf.value.decode(errors="replace") if name_result == 0 else None,
                            "pciBusId": bus_buf.value.decode(errors="replace") if bus_result == 0 else None,
                            "uuid": uuid, "uuidReadbackStatus": uuid_result})
    result.update({"initStatus": init, "deviceCountStatus": count_result, "deviceCount": count.value, "devices": devices})
    return lib, result


def load_runtime():
    name = ctypes.util.find_library("cudart")
    if not name:
        return {"library": None}
    lib = ctypes.CDLL(name)

    def call(function: str, *args):
        fn = getattr(lib, function)
        fn.restype = ctypes.c_int
        return fn(*args)

    count = ctypes.c_int()
    count_status = call("cudaGetDeviceCount", ctypes.byref(count))
    devices = []
    if count_status == 0:
        for ordinal in range(count.value):
            bus = ctypes.create_string_buffer(32)
            bus_status = call("cudaDeviceGetPCIBusId", bus, len(bus), ordinal)
            devices.append({"logicalOrdinal": ordinal, "pciBusId": bus.value.decode(errors="replace") if bus_status == 0 else None,
                            "pciBusIdStatus": bus_status})
    current = ctypes.c_int(-1)
    current_status = call("cudaGetDevice", ctypes.byref(current))
    if count_status == 0 and count.value:
        set_status = call("cudaSetDevice", 0)
        current_after = ctypes.c_int(-1)
        current_after_status = call("cudaGetDevice", ctypes.byref(current_after))
    else:
        set_status = None
        current_after = ctypes.c_int(-1)
        current_after_status = None
    return {"library": name, "deviceCountStatus": count_status, "deviceCount": count.value,
            "devices": devices, "currentBefore": current.value if current_status == 0 else None,
            "currentStatus": current_status, "setFirstDeviceStatus": set_status,
            "currentAfter": current_after.value if current_after_status == 0 else None,
            "currentAfterStatus": current_after_status}


def command(argv: list[str]) -> dict[str, object]:
    try:
        completed = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=8)
        return {"argv": argv, "returnCode": completed.returncode, "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": argv, "error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    import viennaps as ps
    import viennals as ls

    driver, driver_info = load_driver()
    runtime_info = load_runtime()
    drm = []
    for path in sorted(Path("/sys/class/drm").glob("card[0-9]*/device")):
        item: dict[str, object] = {"path": str(path)}
        for key in ("vendor", "device", "subsystem_vendor", "subsystem_device"):
            try:
                item[key] = (path / key).read_text().strip()
            except OSError:
                pass
        drm.append(item)
    visible = {key: os.environ.get(key) for key in ("CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER")}
    gpu_available = getattr(ps, "gpuAvailable", None)
    if callable(gpu_available):
        try:
            gpu_available = gpu_available()
        except BaseException as exc:
            gpu_available = f"call failed: {type(exc).__name__}: {exc}"
    output = {
        "installedVersions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
        "environment": visible,
        "viennapsGpuAvailable": gpu_available,
        "viennapsGpuNames": sorted(name for name in dir(ps) if any(token in name.lower() for token in ("gpu", "cuda", "device"))),
        "viennalsGpuNames": sorted(name for name in dir(ls) if any(token in name.lower() for token in ("gpu", "cuda", "device"))),
        "cudaDriver": driver_info,
        "cudaRuntime": runtime_info,
        "drmDevices": drm,
        "nvidiaSmiList": command(["nvidia-smi", "--query-gpu=index,name,pci.bus_id,uuid,memory.total,driver_version", "--format=csv,noheader"]),
        "nvidiaSmiComputeApps": command(["nvidia-smi", "--query-compute-apps=pid,process_name,gpu_uuid,used_memory", "--format=csv,noheader"]),
    }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
