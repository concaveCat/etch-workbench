"""Bounded, small-input ViennaPS GPU_DISK capability probe."""

from __future__ import annotations

import json
import sys
import ctypes
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import viennaps as ps

from worker.simulate import mesh_report


def run_operation(operation: str, output_dir: Path) -> dict[str, object]:
    delta = 0.2
    extent = 4.0
    threads = 1
    rays_per_point = 64
    seed = 2771095476
    duration = 0.2
    magnitude = 0.2

    domain = ps.Domain(delta, extent, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    depositing = operation == "deposit"
    if depositing:
        domain.duplicateTopLevelSet(ps.Material.SiO2)
    rate = magnitude if depositing else -magnitude
    model = ps.SingleParticleProcess(
        rate=rate,
        stickingProbability=0.5,
        sourceExponent=1.0,
        maskMaterial=ps.Material.Mask,
    )
    process = ps.Process(domain, model, duration)
    process.setFluxEngineType(ps.FluxEngineType.GPU_DISK)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = 0.25
    process.setParameters(advection)
    rays = ps.RayTracingParameters()
    rays.raysPerPoint = rays_per_point
    rays.rngSeed = seed
    rays.useRandomSeeds = False
    process.setParameters(rays)
    process.apply()

    mesh = mesh_report(domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01))
    material_ids = sorted({int(value) for value in mesh["cellData"]["MaterialIds"]})
    vtk_path = output_dir / f"gpu-disk-{operation}.vtk"
    domain.saveSurfaceMesh(str(vtk_path), addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    return {
        "operation": operation,
        "engine": "GPU_DISK",
        "gridDeltaRequested": delta,
        "gridDeltaReadback": float(domain.getGridDelta()),
        "xExtent": extent,
        "threads": threads,
        "raysPerPoint": int(rays.raysPerPoint),
        "seed": int(rays.rngSeed),
        "useRandomSeeds": bool(rays.useRandomSeeds),
        "actualRate": rate,
        "duration": duration,
        "timeStepRatio": float(advection.timeStepRatio),
        "pointCount": len(mesh["points"]),
        "lineCount": len(mesh["lines"]),
        "materialIds": material_ids,
        "vtkPath": str(vtk_path),
    }


def cuda_device_info() -> dict[str, object]:
    driver = ctypes.CDLL("libcuda.so.1")
    driver.cuInit.argtypes = [ctypes.c_uint]
    driver.cuInit.restype = ctypes.c_int
    driver.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
    driver.cuDeviceGetCount.restype = ctypes.c_int
    driver.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
    driver.cuDeviceGet.restype = ctypes.c_int
    driver.cuDeviceGetName.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    driver.cuDeviceGetName.restype = ctypes.c_int
    if driver.cuInit(0) != 0:
        raise RuntimeError("CUDA driver cuInit failed after ViennaPS gpuAvailable() succeeded")
    count = ctypes.c_int()
    if driver.cuDeviceGetCount(ctypes.byref(count)) != 0:
        raise RuntimeError("CUDA driver could not report the device count")
    names = []
    for index in range(count.value):
        device = ctypes.c_int()
        if driver.cuDeviceGet(ctypes.byref(device), index) != 0:
            raise RuntimeError(f"CUDA driver could not open device {index}")
        name = ctypes.create_string_buffer(256)
        if driver.cuDeviceGetName(name, len(name), device.value) != 0:
            raise RuntimeError(f"CUDA driver could not read device {index} name")
        names.append(name.value.decode("utf-8", errors="replace"))
    return {"deviceCount": count.value, "deviceNames": names}


def main() -> None:
    if not bool(ps.gpuAvailable()):
        raise RuntimeError("ViennaPS gpuAvailable() returned false")
    ps.setDimension(2)
    ps.setNumThreads(1)
    output_dir = Path(sys.argv[1]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "versions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
        "gpuAvailable": True,
        "cuda": cuda_device_info(),
        "results": [run_operation(operation, output_dir) for operation in ("etch", "deposit")],
    }
    (output_dir / "gpu-engine-results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
