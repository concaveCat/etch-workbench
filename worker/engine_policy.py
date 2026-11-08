"""Engine selection and multi-ion GPU policy for the bounded worker.

The worker is reachable directly through a run directory, so it re-applies the
gates that ``backend.validation`` and ``backend.supervisor`` apply to API and
queued requests: no silent engine downgrade, no unproven GPU_TRIANGLE call and
no multi-ion GPU flux on a runtime that converts one ion per native call.
"""

from __future__ import annotations

import math
from typing import Any

from backend.engine import multi_ion_gpu_error
from backend.validation import GPU_MULTIPARTICLE_FLUX_STRATEGY, capability_verified, gpu_project_signature


def requested_engine_for_step(project: dict[str, Any], step: dict[str, Any], step_engines: dict[str, Any]) -> str:
    """Resolve one worker engine without ever contradicting solver.gpuEngine.

    Direct worker input does not pass through backend.validation, so an
    omitted ``stepEngines`` entry must not silently downgrade an explicit
    schema-7 GPU engine to GPU_DISK. A pinned entry that disagrees with the
    requested engine is a hard error.
    """
    explicit = step_engines.get(step["id"])
    solver = project.get("solver") or {}
    requested = solver.get("gpuEngine") if project.get("schemaVersion") == 7 else None
    if requested is not None:
        if solver.get("enginePreference") != "gpu":
            raise RuntimeError("solver.gpuEngine requires solver.enginePreference=gpu in the worker input")
        if explicit is not None and explicit != requested:
            raise RuntimeError(
                f"pinned engine {explicit} for step {step['id']} differs from solver.gpuEngine {requested}"
            )
        return str(requested)
    if explicit is not None:
        return str(explicit)
    return "GPU_DISK" if solver.get("enginePreference") == "gpu" else "CPU_DISK"


def verify_worker_engine_evidence(project: dict[str, Any], step: dict[str, Any], engine: str) -> None:
    """Apply the API capability gate to direct worker input.

    The worker is independently reachable through a bounded run directory.
    GPU_TRIANGLE must be checked against the archived product/direct matrix
    here too, otherwise a normalized but unproven project could still call
    Process.setFluxEngineType(GPU_TRIANGLE).
    """
    if engine != "GPU_TRIANGLE":
        return
    model = f"specialized:{step['nativeModel']}" if step.get("model") == "specialized" else step["model"]
    boundary = project["domain"].get("boundaryXY", project["domain"].get("xBoundary"))
    signature = gpu_project_signature(project, step)
    if not capability_verified(
        project["dimension"],
        model,
        step["operation"],
        boundary,
        engine,
        schema_version=project.get("schemaVersion"),
        **signature,
    ):
        raise RuntimeError(
            f"GPU_TRIANGLE has no direct-reference acceptance row for {model} "
            f"{step['operation']} on {boundary} in {project['dimension']}D; the worker refuses "
            "the request instead of falling back to GPU_DISK or CPU_DISK"
        )


def native_slice_position(position: float, grid_delta: float) -> float:
    """Reproduce ViennaLS Slice's std::round(position/gridDelta)*gridDelta.

    C++ std::round rounds exact halves away from zero. Comparing the
    fractional part avoids ``floor(q + 0.5)`` accidentally rounding
    0.49999999999999994 up to 1.0 when the addition itself is rounded.
    """
    quotient = position / grid_delta
    if quotient >= 0.0:
        lower = math.floor(quotient)
        index = lower + 1.0 if quotient - lower >= 0.5 else lower
    else:
        upper = math.ceil(quotient)
        index = upper - 1.0 if upper - quotient >= 0.5 else upper
    result = index * grid_delta
    if index == 0.0 and math.copysign(1.0, quotient) < 0.0:
        return -0.0
    return result


def multi_ion_gpu_step(step: dict[str, Any], engine: str | None) -> bool:
    """True when a step asks a GPU flux engine to convert more than one ion."""
    if step.get("model") != "multiParticle" or not str(engine or "").startswith("GPU_"):
        return False
    return sum(row.get("kind") == "ion" for row in step.get("species", [])) > 1


def require_multi_ion_gpu_policy(step: dict[str, Any], engine: str | None) -> None:
    """Shared >= 4.7.0 multi-ion GPU policy for the single-pass flux path.

    Applied before any native model or GPU flux call and independently of the
    recorded flux strategy: on 4.7.0 every GPU flux engine converts one ion per
    native call, so a multi-ion flux is either silently truncated (stock native
    call) or unverified (per-species merge, which has 4.6.2 evidence only).
    ``backend.validation`` rejects API requests and ``backend.supervisor``
    rejects queued runs with the same helper, but a direct worker caller reaches
    this function without either, so the gate lives here as well.
    """
    if not multi_ion_gpu_step(step, engine):
        return
    policy = multi_ion_gpu_error()
    if policy is not None:
        raise RuntimeError(policy)


def require_multi_ion_flux_fields(report: dict[str, Any], step: dict[str, Any], flux_strategy: str | None) -> None:
    """Reject a native GPU flux mesh that dropped species fields.

    Applies to the single native ``calculateFlux()`` call, including the
    ``flux_strategy is None`` case: an unstrategised multi-ion GPU flux is not
    "no strategy needed", it is the exact configuration where the installed
    binding returns one ``<label><index>`` field and silently ignores the rest.
    The per-species merge route builds its own fields and is checked by the
    merge, not here.
    """
    if flux_strategy == GPU_MULTIPARTICLE_FLUX_STRATEGY:
        return
    expected_fields = {f"{row['label']}{index}" for index, row in enumerate(step["species"])}
    present_fields = set(report["pointData"]["scalars"]) | set(report["cellData"]["scalars"])
    if missing := expected_fields - present_fields:
        raise RuntimeError(
            f"native GPU MultiParticle flux is missing species fields: {sorted(missing)} "
            f"(present: {sorted(present_fields)}); the installed binding returned a truncated "
            f"multi-ion flux, so the run is rejected instead of reporting a partial result"
        )
