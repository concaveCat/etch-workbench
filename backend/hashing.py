"""Stable request and execution hashes for saved runs."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable

from .models import Project, ProjectV3, ProjectV4, ProjectV5, ProjectV6, ProjectV7, Step
from .rates import has_motion, uses_flux


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _step_value(step: Any, key: str) -> Any:
    return step.get(key) if isinstance(step, dict) else getattr(step, key)


def _motion(step: Any, project: Any) -> bool:
    return has_motion(step) if project.schemaVersion in (4, 5, 6, 7) else _step_value(step,"rateMagnitude")>0


def _applicable_solver(project: Project | ProjectV3 | ProjectV4 | ProjectV5 | ProjectV6 | ProjectV7) -> dict[str, Any]:
    active_steps = [step for step in project.steps if _step_value(step,"enabled") and _motion(step,project)]
    if not active_steps:
        return {}
    data: dict[str, Any] = {"timeStepRatio": project.solver.timeStepRatio, "cpuThreads": project.solver.cpuThreads}
    if project.schemaVersion == 7:
        data["advection"] = project.solver.advection.model_dump(mode="json")
        if project.solver.gpuDeviceId is not None:
            data["gpuDeviceId"] = project.solver.gpuDeviceId
        if project.solver.gpuEngine is not None:
            # Engine identity is part of the requested simulation input: a
            # GPU_TRIANGLE request must never hash the same as GPU_DISK.
            data["gpuEngine"] = project.solver.gpuEngine
        if project.solver.engineId is not None:
            data["engineId"] = project.solver.engineId
            data["engineKernel"] = project.solver.engineKernel
            if project.solver.engineBackend is not None:
                data["engineBackend"] = project.solver.engineBackend
    if any((uses_flux(step) if project.schemaVersion in (4, 5, 6, 7) else _step_value(step,"model")=="singleParticle" and _step_value(step,"rateMagnitude")>0) for step in project.steps if _step_value(step,"enabled")):
        data.update(
            raysPerPoint=project.solver.raysPerPoint,
            random=project.solver.random.model_dump(mode="json", exclude_none=True),
            enginePreference=project.solver.enginePreference,
        )
        if project.schemaVersion == 7:
            data["rayTracing"] = project.solver.rayTracing.model_dump(mode="json")
    return data


def requested_input(project: Project | ProjectV3 | ProjectV4 | ProjectV5 | ProjectV6 | ProjectV7) -> dict[str, Any]:
    geometry = json.loads(json.dumps(project.geometry)) if isinstance(project.geometry,dict) else project.geometry.model_dump(mode="json", exclude_none=True)
    if project.schemaVersion in (3,4,5,6,7) and geometry.get("kind") == "stack":
        pattern = geometry.get("pattern") or {}
        if pattern.get("layers"):
            # Layer IDs are deliberately hash-neutral. Resolve ID references to
            # positions before the IDs are removed so a pure rename keeps the
            # request hash stable for per-layer stacks too.
            by_id = {layer["id"]: index for index, layer in enumerate(geometry["layers"])}
            for entry in pattern["layers"]:
                if entry.get("layerId") is not None:
                    entry["layerIndex"] = by_id[entry["layerId"]]
                    entry.pop("layerId", None)
        for layer in geometry["layers"]:
            layer.pop("id", None)
            layer.pop("name", None)
    steps = []
    for step in project.steps:
        if not _step_value(step,"enabled"):
            continue
        item = step if isinstance(step,dict) else step.model_dump(mode="json", exclude_none=True)
        item = dict(item)
        item.pop("id", None)
        item.pop("name", None)
        item.pop("enabled", None)
        if project.schemaVersion in (4,5,6,7):
            item.pop("selectivityReference",None)
        if project.schemaVersion in (5,6,7) and item.get("model") == "directional":
            material_mode = (item.get("materialRates") is not None
                             or item.get("defaultMaterialRate") is not None)
            if material_mode:
                # The native RateMap consumes the direction even when only an
                # isotropic component is nonzero; unlike the scalar overload
                # there is no visibility flag to drop. Keep the normalized
                # direction in the hash.
                direction = item.get("direction", [0.0, 0.0, 0.0])
                norm = math.sqrt(sum(float(value) ** 2 for value in direction))
                if norm > 0.0:
                    item["direction"] = [float(value) / norm for value in direction]
            elif item.get("rateSets") is not None:
                # The multi-set surface is additive: each set normalizes its own
                # direction, and a set without a directional magnitude cannot
                # consume its direction or visibility natively. Mirroring the
                # scalar rule keeps hashes equal for physically equal inputs.
                for rate_set in item["rateSets"]:
                    if float(rate_set.get("directionalVelocity", 0.0)) == 0.0:
                        rate_set.pop("direction", None)
                        rate_set.pop("calculateVisibility", None)
                    else:
                        direction = rate_set.get("direction", [0.0, 0.0, 0.0])
                        norm = math.sqrt(sum(float(value) ** 2 for value in direction))
                        if norm > 0.0:
                            rate_set["direction"] = [float(value) / norm for value in direction]
            elif float(item.get("directionalRateMagnitude", 0.0)) == 0.0:
                item.pop("direction", None)
                item.pop("calculateVisibility", None)
            else:
                direction = item.get("direction", [0.0, 0.0, 0.0])
                norm = math.sqrt(sum(float(value) ** 2 for value in direction))
                if norm > 0.0:
                    item["direction"] = [float(value) / norm for value in direction]
            if not has_motion(step):
                item.pop("operation", None)
                item.pop("depositMaterial", None)
        elif project.schemaVersion not in (4,5,6,7) and _step_value(step,"rateMagnitude") == 0:
            for key in ("model", "depositMaterial", "stickingProbability", "sourceExponent"):
                item.pop(key, None)
        steps.append(item)
    result = {
        "dimension": project.dimension,
        "units": project.units.model_dump(mode="json"),
        "domain": project.domain.model_dump(mode="json", exclude_none=True),
        "geometry": geometry,
        "steps": steps,
        "solver": _applicable_solver(project),
    }
    if isinstance(project, (ProjectV6, ProjectV7)) and project.continuationState is not None:
        # Provenance changes when a portable state package moves workbenches;
        # the content hash and compatibility metadata define simulation input.
        result["continuationState"] = project.continuationState.model_dump(mode="json", exclude_none=True, exclude={"sourceRunId", "sourceStepId"})
    if project.schemaVersion == 7 and project.outputSettings is not None:
        # ``cellSet`` is a new optional block. Excluding None keeps every
        # historical schema-7 hash stable when the block is omitted.
        result["outputSettings"] = project.outputSettings.model_dump(mode="json", exclude_none=True)
    definitions = getattr(project, "materialDefinitions", None) or []
    if definitions:
        from .materials import referenced_material_names
        used = referenced_material_names(project)
        result["customMaterials"] = [
            row.model_dump(mode="json", exclude_none=True)
            for row in definitions if row.name in used
        ]
    return result


def requested_input_hash(project: Project | ProjectV3 | ProjectV4 | ProjectV5 | ProjectV6 | ProjectV7) -> str:
    return hashlib.sha256(_canonical_bytes(requested_input(project))).hexdigest()


def execution_hash(
    request_hash: str,
    *,
    run_seed: int | None,
    cpu_threads: int,
    step_engines: Iterable[dict[str, Any]],
    viennaps_version: str,
    viennals_version: str,
    native_engine_fingerprint: str | None = None,
) -> str:
    payload = {
        "requestedInputHash": request_hash,
        "runSeed": run_seed,
        "cpuThreads": cpu_threads,
        "steps": list(step_engines),
        "viennapsVersion": viennaps_version,
        "viennalsVersion": viennals_version,
    }
    if native_engine_fingerprint is not None:
        payload["nativeEngineFingerprint"] = native_engine_fingerprint
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()
