"""Bounded probe: stock (unpatched) MultiParticle multi-ion GPU on the installed runtime.

Answers one question per run: on the *installed* viennaps wheel, does a
MultiParticleProcess with more than one ion species actually convert every ion
species into its own flux field on CPU_DISK / GPU_DISK / GPU_TRIANGLE?

The 4.6.2 GPU binding silently dropped every ion after the first, which is why
the Workbench injects its patched runtime. This probe measures the stock
behaviour instead of assuming it.

Discriminator: the rate callback depends only on the *second* ion (weight 0 for
ion0). If the engine ignores ion1 the callback sees a zero/short flux vector
and the surface does not move.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import sys
import traceback
from pathlib import Path

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "migration-probes"))
from common import write  # noqa: E402

ION_KEYS = ("sourcePower", "thetaRMin", "thetaRMax", "minAngle", "B_sp",
            "meanEnergy", "sigmaEnergy", "thresholdEnergy", "inflectAngle", "n")


def ion(index: int) -> dict:
    """Deterministic, clearly distinct ion distributions."""
    return {
        "sourcePower": 1.0,
        "thetaRMin": -80.0,
        "thetaRMax": 80.0,
        "minAngle": -60.0 + 10.0 * index,
        "B_sp": 35.0 + 5.0 * index,
        "meanEnergy": 500.0 + 250.0 * index,
        "sigmaEnergy": 40.0 + 5.0 * index,
        "thresholdEnergy": 20.0 + 10.0 * index,
        "inflectAngle": 30.0,
        "n": 2.0 + index,
    }


def surface_stats(mesh, axis: int) -> dict:
    points = list(mesh.getNodes())
    values = [float(point[axis]) for point in points]
    return {"points": len(points),
            "min": min(values), "max": max(values),
            "mean": sum(values) / len(values),
            "hash": hashlib.sha256(
                repr(sorted(tuple(round(float(c), 9) for c in p) for p in points)).encode()
            ).hexdigest()}


def build_domain(dimension: int):
    if dimension == 2:
        domain = ps.Domain(0.2, 4.0, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    else:
        domain = ps.Domain(0.2, 4.0, 4.0, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def run(dimension: int, engine: str, ions: int, rays: int, active_index: int) -> dict:
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    ps.Logger.setLogLevel(ps.LogLevel.ERROR)
    device = None
    if engine.startswith("GPU_"):
        from backend.gpu_devices import cuda_devices, resolve_gpu_device
        from worker.simulate import cuda_runtime_readback
        device = resolve_gpu_device(os.environ.get("CUDA_VISIBLE_DEVICES"),
                                    cuda_devices(refresh=True))
        ps.gpu.Context.createContext()

    domain = build_domain(dimension)
    model = ps.MultiParticleProcess() if dimension == 2 else ps.d3.MultiParticleProcess()
    labels = [f"ion{i}" for i in range(ions)]
    for index, label in enumerate(labels):
        params = ion(index)
        model.addIonParticle(**{key: params[key] for key in ION_KEYS}, label=label)

    weights = [1.0 if i == active_index else 0.0 for i in range(ions)]
    flux_sums = [0.0] * ions
    flux_maxima = [0.0] * ions
    vector_lengths: set[int] = set()
    callbacks = 0

    def rate_function(fluxes, _material):
        nonlocal callbacks
        values = [float(value) for value in fluxes]
        vector_lengths.add(len(values))
        if len(values) > ions:
            raise RuntimeError(f"flux vector longer than the {ions} configured ions: {values!r}")
        for index, value in enumerate(values):
            flux_sums[index] += value
            flux_maxima[index] = max(flux_maxima[index], abs(value))
        callbacks += 1
        return -0.02 * sum(value * weights[index] for index, value in enumerate(values))

    model.setRateFunction(rate_function)
    process = ps.Process(domain, model, 0.1)
    process.setFluxEngineType(getattr(ps.FluxEngineType, engine))
    ray_parameters = ps.RayTracingParameters()
    ray_parameters.raysPerPoint = rays
    ray_parameters.rngSeed = 314159
    ray_parameters.useRandomSeeds = False
    process.setParameters(ray_parameters)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = 0.25
    process.setParameters(advection)

    initial = surface_stats(domain.getSurfaceMesh(), 1 if dimension == 2 else 2)
    flux_mesh = process.calculateFlux()
    data = flux_mesh.getCellData()
    fields = {str(data.getScalarDataLabel(i)): [float(v) for v in data.getScalarData(i)]
              for i in range(data.getScalarDataSize())}
    process.apply()
    final = surface_stats(domain.getSurfaceMesh(), 1 if dimension == 2 else 2)
    if device is not None:
        device = {"uuid": device["uuid"], "readback": cuda_runtime_readback(device)}
    return {
        "engine": engine, "dimension": dimension, "ions": ions,
        "activeIonIndex": active_index, "raysPerPoint": rays,
        "speciesLabels": labels,
        "callbacks": callbacks, "fluxVectorLengths": sorted(vector_lengths),
        "fluxSumByIon": flux_sums, "fluxMaxByIon": flux_maxima,
        "fluxFieldNames": sorted(fields),
        "fluxFieldMaxByIon": {key: max((abs(v) for v in value), default=0.0)
                              for key, value in fields.items()},
        "initial": initial, "final": final,
        "meanDisplacement": initial["mean"] - final["mean"],
        "surfaceChanged": initial["hash"] != final["hash"],
        "device": device,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dimension", type=int, choices=(2, 3), required=True)
    parser.add_argument("--engine", choices=("CPU_DISK", "GPU_DISK", "GPU_TRIANGLE"), required=True)
    parser.add_argument("--ions", type=int, default=2)
    parser.add_argument("--rays", type=int, default=96)
    parser.add_argument("--active-index", type=int, default=1)
    parser.add_argument("--repeat", action="store_true", help="run twice and compare replay determinism")
    args = parser.parse_args()
    payload = {
        "versions": {"viennaps": importlib.metadata.version("viennaps"),
                     "viennals": importlib.metadata.version("viennals")},
        "viennapsModulePath": ps.__file__,
        "gpuAvailable": bool(ps.gpuAvailable()),
        "cudaVisibleDevices": os.environ.get("CUDA_VISIBLE_DEVICES"),
    }
    try:
        first = run(args.dimension, args.engine, args.ions, args.rays, args.active_index)
        payload["result"] = first
        if args.repeat:
            second = run(args.dimension, args.engine, args.ions, args.rays, args.active_index)
            payload["replay"] = {
                "identicalHash": first["final"]["hash"] == second["final"]["hash"],
                "first": first["final"]["hash"], "second": second["final"]["hash"],
            }
        payload["status"] = "succeeded"
    except Exception as exc:  # noqa: BLE001 - probe records the native failure
        payload["status"] = "failed"
        payload["error"] = f"{type(exc).__name__}: {exc}"
        payload["traceback"] = traceback.format_exc()[-4000:]
    write(str(args.output), payload)
    print(json.dumps({"status": payload["status"], "engine": args.engine,
                      "dimension": args.dimension, "ions": args.ions,
                      "error": payload.get("error"), "output": str(args.output)}), flush=True)
    if payload["status"] != "succeeded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
