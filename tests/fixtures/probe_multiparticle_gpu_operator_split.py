"""Bounded proof of concept for multi-ion GPU operator splitting.

Each native GPU sub-process contains exactly one particle species, avoiding
ViennaPS 4.6.2's GPU binding limit of one ion. Symmetric composition preserves
the requested integrated velocity and gives a second-order split of the native
multi-particle evolution as the sweep count increases.
"""
from __future__ import annotations

import importlib.metadata
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from stage7_multiparticle_reference import (  # noqa: E402
    direct_material,
    make_domain,
    project_material_names,
    register_custom_materials,
    snapshot,
)


def add_one_species(model: Any, species: dict[str, Any]) -> None:
    label = str(species["label"])
    if species["kind"] == "ion":
        model.addIonParticle(
            sourcePower=float(species["sourcePower"]),
            thetaRMin=float(species["thetaRMin"]), thetaRMax=float(species["thetaRMax"]),
            minAngle=float(species["minAngle"]), B_sp=float(species["B_sp"]),
            meanEnergy=float(species["meanEnergy"]), sigmaEnergy=float(species["sigmaEnergy"]),
            thresholdEnergy=float(species["thresholdEnergy"]), inflectAngle=float(species["inflectAngle"]),
            n=int(species["n"]), label=label,
        )
        return
    sticking = {
        direct_material(name): float(value)
        for name, value in species.get("stickingProbabilityByMaterial", {}).items()
    }
    model.addNeutralParticle(
        sticking,
        defaultStickingProbability=float(species["defaultStickingProbability"]),
        label=label,
    )


def split_order(species_count: int) -> list[tuple[int, float]]:
    if species_count == 1:
        return [(0, 1.0)]
    forward = [(index, 0.5) for index in range(species_count - 1)]
    middle = [(species_count - 1, 1.0)]
    backward = [(index, 0.5) for index in reversed(range(species_count - 1))]
    return forward + middle + backward


def make_process_parameters(project: dict[str, Any], seed: int) -> tuple[Any, Any]:
    solver = project["solver"]
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = float(solver["timeStepRatio"])
    requested = solver.get("advection")
    if requested:
        advection.spatialScheme = getattr(ps.ls.SpatialSchemeEnum, requested["spatialScheme"])
        advection.temporalScheme = getattr(ps.ls.TemporalSchemeEnum, requested["temporalScheme"])
        for key in (
            "adaptiveTimeStepping", "adaptiveTimeStepSubdivisions", "calculateIntermediateVelocities",
            "checkDissipation", "dissipationAlpha", "ignoreVoids", "velocityOutput",
        ):
            setattr(advection, key, requested[key])

    ray = ps.RayTracingParameters()
    ray.raysPerPoint = int(solver["raysPerPoint"])
    ray.rngSeed = int(seed)
    ray.useRandomSeeds = False
    for key, value in solver.get("rayTracing", {}).items():
        if key == "normalizationType":
            ray.normalizationType = getattr(ps.NormalizationType, value)
        elif hasattr(ray, key):
            setattr(ray, key, value)
    return advection, ray


def run_split(project: dict[str, Any], sweeps: int) -> dict[str, Any]:
    if sweeps < 1:
        raise ValueError("sweeps must be positive")
    dimension = int(project["dimension"])
    step = next(row for row in project["steps"] if row["enabled"])
    species = list(step["species"])
    if len(species) < 2:
        raise ValueError("the proof of concept requires at least two species")

    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    ps.setDimension(dimension)
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    register_custom_materials(project.get("materialDefinitions", []))
    domain = make_domain(project)
    initial = snapshot(domain, dimension)
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(direct_material(step["depositMaterial"]))

    labels = [str(row["label"]) for row in species]
    relation = step["rateCoefficients"]
    defaults = {label: float(relation["defaultBySpecies"][label]) for label in labels}
    overrides = {
        material: {label: float(value) for label, value in values.items()}
        for material, values in relation.get("byMaterial", {}).items()
    }
    material_names = {str(direct_material(name)): name for name in project_material_names(project)}
    sign = -1.0 if step["operation"] == "etch" else 1.0
    order = split_order(len(species))
    callbacks: Counter[str] = Counter()
    stage_rows = []
    base_seed = int(project["solver"]["random"]["seed"])
    global_stage = 0
    duration = float(step["duration"])
    started = time.perf_counter()

    for sweep in range(sweeps):
        for species_index, fraction in order:
            particle = species[species_index]
            label = labels[species_index]
            native_duration = duration * fraction / sweeps
            callback_count = 0

            def rate_function(fluxes: Any, material: Any) -> float:
                nonlocal callback_count
                material_name = material_names.get(str(material))
                if material_name is None:
                    raise RuntimeError(f"unregistered material in rate callback: {material}")
                values = [float(value) for value in fluxes]
                if len(values) != 1 or not math.isfinite(values[0]):
                    raise RuntimeError(f"single-species GPU callback has unexpected fluxes: {values!r}")
                coefficient = overrides.get(material_name, {}).get(label, defaults[label])
                callback_count += 1
                return sign * values[0] * coefficient

            model = ps.MultiParticleProcess()
            add_one_species(model, particle)
            model.setRateFunction(rate_function)
            process = ps.Process(domain, model, native_duration)
            process.setFluxEngineType(ps.FluxEngineType.GPU_DISK)
            advection, ray = make_process_parameters(project, base_seed + global_stage)
            process.setParameters(advection)
            process.setParameters(ray)
            stage_started = time.perf_counter()
            process.apply()
            elapsed = time.perf_counter() - stage_started
            if callback_count <= 0:
                raise RuntimeError(f"ViennaPS did not call the {label!r} rate relation")
            callbacks[label] += callback_count
            stage_rows.append({
                "sweep": sweep,
                "species": label,
                "fraction": fraction,
                "duration": native_duration,
                "seed": base_seed + global_stage,
                "engine": "GPU_DISK",
                "callbackCount": callback_count,
                "elapsedSeconds": elapsed,
            })
            global_stage += 1

    total = time.perf_counter() - started
    return {
        "sweeps": sweeps,
        "order": order,
        "duration": duration,
        "totalElapsedSeconds": total,
        "callbacksBySpecies": dict(callbacks),
        "stages": stage_rows,
        "initial": initial,
        "final": snapshot(domain, dimension),
    }


def main(project_path: Path, output_path: Path, sweeps: list[int]) -> None:
    project = json.loads(project_path.read_text(encoding="utf-8"))
    result = {
        "status": "succeeded",
        "engine": "GPU_DISK",
        "installedVersions": {
            "viennaps": importlib.metadata.version("viennaps"),
            "viennals": importlib.metadata.version("viennals"),
        },
        "contextDeviceIndex": int(ps.gpu.Context.createContext().deviceID),
        "dimension": int(project["dimension"]),
        "species": [{"label": row["label"], "kind": row["kind"]} for row in project["steps"][0]["species"]],
        "cases": [],
    }
    for value in sweeps:
        result["cases"].append(run_split(project, value))
    output_path.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": result["status"],
        "versions": result["installedVersions"],
        "contextDeviceIndex": result["contextDeviceIndex"],
        "sweeps": [{"sweeps": row["sweeps"], "totalElapsedSeconds": row["totalElapsedSeconds"],
                    "callbacksBySpecies": row["callbacksBySpecies"], "points": len(row["final"]["points"])}
                   for row in result["cases"]],
        "output": str(output_path),
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: probe_multiparticle_gpu_operator_split.py PROJECT.json OUTPUT.json SWEEPS [SWEEPS ...]")
    main(Path(sys.argv[1]), Path(sys.argv[2]), [int(value) for value in sys.argv[3:]])
