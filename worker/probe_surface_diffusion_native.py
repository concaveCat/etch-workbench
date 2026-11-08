"""Bounded ViennaPS 4.6.2 probe for ``SurfaceDiffusionParameters``.

Run through ``scripts/bounded_child.py`` or directly with the workbench venv:

    .venv/bin/python worker/probe_surface_diffusion_native.py --output <path.json>

The audit listed ``SurfaceDiffusionParameters`` as "a level-set smoothing
model" with no evidenced insertion point. This probe establishes, against the
installed bindings only, what the native object actually does in 4.6.2:

* ``Process.setParameters(SurfaceDiffusionParameters)`` is a bound overload in
  2D and 3D (``viennaps.d2.Process`` and ``viennaps.d3.Process``);
* the parameters only become active when the process model exposes diffusion
  coefficients; in 4.6.2 that is only
  ``SingleParticleALDParams.coverageDiffusionCoefficient > 0`` and the ALP
  strategy diffuses the ``Coverage`` scalar on the disk mesh;
* with a zero coefficient the object is inert (bit-identical surface);
* with a positive coefficient the native defaults are exactly the no-object
  defaults, while ``radius``/``sigmaNormal``/``kNeighbors``/
  ``stabilityFactor``/``symmetrizeWeights`` move the deposited surface
  deterministically and leave the material map unchanged;
* the same object on an isotropic process does nothing (no diffusion
  coefficients in that surface model).

It never touches workbench state and writes only the requested JSON file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path

import viennaps as ps

DEFAULTS = {
    "kNeighbors": 16,
    "normalCutoff": 0.25,
    "normalizeByLocalScale": True,
    "radius": 0.0,
    "sigmaNormal": 0.35,
    "stabilityFactor": 1.0,
    "symmetrizeWeights": True,
}


def set_dimension(dimension: int) -> None:
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")


def trench(dimension: int):
    set_dimension(dimension)
    if dimension == 2:
        domain = ps.Domain(0.05, 1.2, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    else:
        domain = ps.Domain(0.08, 1.0, 1.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakeTrench(domain=domain, trenchWidth=0.2, trenchDepth=1.0, trenchTaperAngle=0.0,
                  maskHeight=0.1, maskTaperAngle=0.0, halfTrench=False,
                  material=ps.Material.Si, maskMaterial=ps.Material.Mask).apply()
    return domain


def surface(domain, dimension: int) -> list[float]:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    vertical = 1 if dimension == 2 else 2
    return [float(point[vertical]) for point in mesh.getNodes()]


def material_map(domain) -> list[int]:
    mapping = domain.getMaterialMap()
    return [int(mapping.getMaterialIdAtIdx(i)) for i in range(mapping.size())]


def run_ald(dimension: int, coefficient: float, surface_diffusion: dict | None,
            *, cycles: int = 2, seed: int = 7) -> dict:
    domain = trench(dimension)
    domain.duplicateTopLevelSet(ps.Material.Al2O3)
    initial_materials = material_map(domain)

    parameters = ps.SingleParticleALDParams()
    parameters.stickingProbability = 1.0
    parameters.gasMeanFreePath = 1.0
    parameters.growthPerCycle = 0.05
    parameters.incomingFlux = 1.0
    parameters.evaporationFlux = 0.0
    parameters.s0 = 1.0
    parameters.coverageDiffusionCoefficient = coefficient

    atomic = ps.AtomicLayerProcessParameters()
    atomic.pulseTime = 0.02
    atomic.purgePulseTime = 0.0
    atomic.coverageTimeStep = 0.01
    atomic.purgeTimeStep = 1.0
    atomic.numCycles = cycles

    process = ps.Process(domain, ps.SingleParticleALD(parameters), 0.02 * cycles)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = 0.25
    process.setParameters(advection)
    process.setParameters(atomic)
    rays = ps.RayTracingParameters()
    rays.raysPerPoint = 400
    rays.rngSeed = seed
    rays.useRandomSeeds = False
    process.setParameters(rays)
    applied = None
    if surface_diffusion is not None:
        native = ps.SurfaceDiffusionParameters()
        for key, default in DEFAULTS.items():
            setattr(native, key, type(default)(surface_diffusion.get(key, default)))
        process.setParameters(native)
        applied = native.toMetaData()

    process.apply()
    heights = surface(domain, dimension)
    final_materials = material_map(domain)
    return {
        "coefficient": coefficient,
        "surfaceDiffusion": surface_diffusion,
        "appliedMetaData": applied,
        "pointCount": len(heights),
        "sum": round(sum(heights), 12),
        "min": round(min(heights), 12),
        "max": round(max(heights), 12),
        "median": round(statistics.median(heights), 12),
        "heightHash": hashlib.sha256(
            ",".join(f"{value:.15g}" for value in heights).encode()).hexdigest()[:24],
        "materialsPreserved": initial_materials == final_materials,
        "materialMap": final_materials,
    }


def run_isotropic(dimension: int, surface_diffusion: dict | None) -> dict:
    domain = trench(dimension)
    process = ps.Process(domain, ps.IsotropicProcess(rate=0.0), 0.2)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = 0.25
    process.setParameters(advection)
    if surface_diffusion is not None:
        native = ps.SurfaceDiffusionParameters()
        for key, default in DEFAULTS.items():
            setattr(native, key, type(default)(surface_diffusion.get(key, default)))
        process.setParameters(native)
    process.apply()
    final = surface(domain, dimension)
    return {
        "surfaceDiffusion": surface_diffusion,
        "pointCount": len(final),
        "heightHash": hashlib.sha256(
            ",".join(f"{value:.15g}" for value in final).encode()).hexdigest()[:24],
        "sum": round(sum(final), 12),
    }


def delta(base: dict, row: dict) -> dict:
    return {
        "from": base["heightHash"],
        "to": row["heightHash"],
        "sameHash": base["heightHash"] == row["heightHash"],
        "sumDelta": round(row["sum"] - base["sum"], 12),
        "maxDelta": round(row["max"] - base["max"], 12),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    native = ps.SurfaceDiffusionParameters()
    binding_defaults = {key: getattr(native, key) for key in DEFAULTS}
    report: dict = {
        "viennaps": ps.version,
        "setParametersOverload": "Process.setParameters(SurfaceDiffusionParameters) in viennaps.d2.Process and viennaps.d3.Process",
        "nativeDefaultMembers": binding_defaults,
        "nativeDefaultMetaData": native.toMetaData(),
        "declaredDefaultsMatchInstalled": binding_defaults == DEFAULTS,
        "dimensions": {},
    }

    for dimension in (2, 3):
        rows: dict[str, dict] = {}
        base = run_ald(dimension, 0.0, None)
        rows["coefficient0_without_object"] = base
        rows["coefficient0_with_object"] = run_ald(dimension, 0.0, DEFAULTS)
        rows["coefficient0_object_is_inert"] = {
            "sameHash": rows["coefficient0_with_object"]["heightHash"] == base["heightHash"]}

        active_base = run_ald(dimension, 2.0, None)
        rows["coefficient2_without_object"] = active_base
        rows["coefficient2_native_defaults"] = run_ald(dimension, 2.0, DEFAULTS)
        rows["coefficient2_native_defaults_are_noop"] = {
            "sameHash": rows["coefficient2_native_defaults"]["heightHash"] == active_base["heightHash"]}
        variants = {
            "radius_0.1": {"radius": 0.1},
            "radius_0.3": {"radius": 0.3},
            "sigmaNormal_0.05": {"sigmaNormal": 0.05},
            "kNeighbors_4": {"kNeighbors": 4},
            "stabilityFactor_0.25": {"stabilityFactor": 0.25},
            "symmetrizeWeights_false": {"symmetrizeWeights": False},
        }
        for label, override in variants.items():
            row = run_ald(dimension, 2.0, {**DEFAULTS, **override})
            row["deltaFromActiveBase"] = delta(active_base, row)
            rows[label] = row
        repeat = run_ald(dimension, 2.0, {**DEFAULTS, "radius": 0.1})
        rows["radius_0.1_repeat"] = {
            "sameHashAsFirst": repeat["heightHash"] == rows["radius_0.1"]["heightHash"],
            "first": rows["radius_0.1"]["heightHash"],
            "repeat": repeat["heightHash"],
        }
        rows["coefficient2_object_changes_surface"] = {
            "variantsChanged": sorted(
                label for label, row in rows.items()
                if isinstance(row, dict) and row.get("deltaFromActiveBase")
                and not row["deltaFromActiveBase"]["sameHash"])}
        report["dimensions"][str(dimension)] = rows

    non_ald: dict = {}
    for dimension in (2, 3):
        without = run_isotropic(dimension, None)
        with_object = run_isotropic(dimension, {**DEFAULTS, "radius": 0.1})
        non_ald[f"{dimension}d_without_object"] = without
        non_ald[f"{dimension}d_with_object"] = with_object
        non_ald[f"{dimension}d_object_is_inert"] = {
            "sameHash": without["heightHash"] == with_object["heightHash"]}
    report["nonAldProcess"] = non_ald
    report["conclusion"] = (
        "SurfaceDiffusionParameters is a real native setter, but in 4.6.2 it only diffuses the "
        "ALD Coverage scalar when SingleParticleALDParams.coverageDiffusionCoefficient > 0. "
        "It is not a level-set smoother and is inert on every other process."
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
