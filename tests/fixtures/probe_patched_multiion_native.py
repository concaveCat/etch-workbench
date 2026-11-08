"""Exercise a candidate native MultiParticle GPU binding through a guarded child.

The caller controls PYTHONPATH and CUDA_VISIBLE_DEVICES before starting Python.
This probe creates a real native model, checks its flux fields, and applies it.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
import sys
from pathlib import Path

import viennaps as ps
from stage7_multiparticle_reference import direct_material, make_domain, snapshot


def add_species(model, step, force_empty_sticking_map=False):
    labels = []
    for species in step["species"]:
        label = species["label"]
        labels.append(label)
        if species["kind"] == "ion":
            keys = ("sourcePower", "thetaRMin", "thetaRMax", "minAngle", "B_sp",
                    "meanEnergy", "sigmaEnergy", "thresholdEnergy", "inflectAngle", "n")
            model.addIonParticle(**{key: species[key] for key in keys}, label=label)
        else:
            sticking = species.get("stickingProbabilityByMaterial", {})
            default = float(species["defaultStickingProbability"])
            if sticking or force_empty_sticking_map:
                model.addNeutralParticle({direct_material(name): float(value)
                                          for name, value in sticking.items()},
                                         defaultStickingProbability=default, label=label)
            else:
                model.addNeutralParticle(default, label=label)
    return labels


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--route", choices=("generic", "gpu-specific"), required=True)
    parser.add_argument("--engine", choices=("CPU_DISK", "GPU_DISK"), required=True)
    parser.add_argument("--neutral-between-ions", action="store_true")
    parser.add_argument("--xenon-threshold", type=float)
    parser.add_argument("--argon-threshold", type=float)
    parser.add_argument("--force-empty-sticking-map", action="store_true")
    args = parser.parse_args()
    if args.route == "gpu-specific" and args.engine != "GPU_DISK":
        raise ValueError("the GPU-specific class requires GPU_DISK")

    project = json.loads(args.project.read_text(encoding="utf-8"))
    step = project["steps"][0]
    if args.neutral_between_ions:
        species = step["species"]
        if [row["kind"] for row in species] != ["ion", "ion", "neutral"]:
            raise ValueError("probe expects ion, ion, neutral input")
        step["species"] = [species[0], species[2], species[1]]
    if args.xenon_threshold is not None:
        next(row for row in step["species"] if row["label"] == "xenonIon")["thresholdEnergy"] = args.xenon_threshold
    if args.argon_threshold is not None:
        next(row for row in step["species"] if row["label"] == "argonIon")["thresholdEnergy"] = args.argon_threshold

    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    dimension = int(project["dimension"])
    ps.setDimension(dimension)
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    device = None
    device_readback = None
    if args.engine == "GPU_DISK":
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from backend.gpu_devices import cuda_devices, resolve_gpu_device
        from worker.simulate import cuda_runtime_readback, nvidia_process_readback
        device = resolve_gpu_device(os.environ.get("CUDA_VISIBLE_DEVICES"), cuda_devices(refresh=True))
        device_readback = cuda_runtime_readback(device)
        context = ps.gpu.Context.createContext()
    else:
        context = None
    domain = make_domain(project)
    initial = snapshot(domain, dimension)
    binding = ps.d2 if dimension == 2 else ps.d3
    model = (binding.gpu.MultiParticleProcess() if args.route == "gpu-specific"
             else binding.MultiParticleProcess())
    labels = add_species(model, step, args.force_empty_sticking_map)
    samples: list[list[float]] = []
    callback_count = 0

    def rate_function(fluxes, _material):
        nonlocal callback_count
        values = [float(value) for value in fluxes]
        if len(values) != len(labels) or not all(math.isfinite(value) for value in values):
            raise RuntimeError(f"unexpected native flux vector: {values!r}")
        callback_count += 1
        if len(samples) < 32:
            samples.append(values)
        return -0.025 * sum(values)

    model.setRateFunction(rate_function)
    process = ps.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(getattr(ps.FluxEngineType, args.engine))
    rays = ps.RayTracingParameters()
    rays.raysPerPoint = int(project["solver"]["raysPerPoint"])
    rays.rngSeed = int(project["solver"]["random"]["seed"])
    rays.useRandomSeeds = False
    for key, value in project["solver"].get("rayTracing", {}).items():
        if key == "normalizationType":
            rays.normalizationType = getattr(ps.NormalizationType, value)
        elif hasattr(rays, key):
            setattr(rays, key, value)
    process.setParameters(rays)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
    process.setParameters(advection)
    mesh = process.calculateFlux()
    data = mesh.getCellData()
    fields = {str(data.getScalarDataLabel(i)): [float(v) for v in data.getScalarData(i)]
              for i in range(data.getScalarDataSize())}
    expected = [f"{label}{index}" for index, label in enumerate(labels)]
    missing = [name for name in expected if name not in fields]
    if missing:
        raise RuntimeError(f"native flux fields missing: {missing}; actual: {sorted(fields)}")
    if any(not values or not all(math.isfinite(value) for value in values)
           for name, values in fields.items() if name in expected):
        raise RuntimeError("native particle fields are empty or non-finite")
    process.apply()
    if callback_count == 0:
        raise RuntimeError("native process did not invoke the rate callback")
    if device:
        device_readback["afterProcessApply"] = cuda_runtime_readback(device)
        device_readback["process"] = nvidia_process_readback(device["uuid"])
    result = {"status": "succeeded", "engine": args.engine, "route": args.route,
              "dimension": dimension, "versions": {"viennaps": importlib.metadata.version("viennaps"),
              "viennals": importlib.metadata.version("viennals")},
              "contextDeviceIndex": int(context.deviceID) if context else None,
              "viennapsModulePath": ps.__file__, "cudaDeviceReadback": device_readback,
              "speciesOrder": labels, "expectedFields": expected,
              "particleFields": {name: fields[name] for name in expected},
              "rateCallbackCount": callback_count, "rateCallbackSamples": samples,
              "initial": initial, "final": snapshot(domain, dimension)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "engine": args.engine, "route": args.route,
                      "dimension": dimension, "fields": expected,
                      "rateCallbackCount": callback_count, "output": str(args.output)}), flush=True)


if __name__ == "__main__":
    main()
