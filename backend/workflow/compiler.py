"""Deterministic P0 compiler from a workflow flow to the frozen execution schema."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from ..models import ProjectV7, StepV7, View, View3DV3
from .hashing import compile_record_id, full_project_hash, source_map_hash
from .ids import STEP_ID_POLICY, sha256_hex, stable_step_id
from .models import (
    CompiledWorkflowV1,
    CompileRecordV1,
    FlowRevisionV1,
    GeometryBindingV1,
    ModuleRevisionRefV1,
    ParameterProvenanceV1,
    SourceMapEntryV1,
)

COMPILER_VERSION = "0.1.0"

_FIELD_MODELS = frozenset({"implant", "anneal", "analysis"})


def _normalized_step_payloads(flow: FlowRevisionV1) -> list[dict[str, Any]]:
    """Return step payloads with one canonical cellSet per field sequence.

    ``implant -> anneal -> analysis`` is one contiguous field sequence. Every
    step in it must run on the same native lattice: ``height`` and
    ``substrateDepth`` take the sequence maximum, while ``coverMaterial`` and
    ``periodic`` must already agree. Normalizing here, before the project hash
    is computed, means two flows that differ only in a per-step grid setting
    still compile to the same execution project.
    """
    payloads = [step.model_dump(mode="json", exclude_none=True) for step in flow.steps]
    run: list[int] = []
    for index, payload in enumerate(payloads):
        if not _is_enabled(payload):
            # A disabled step never executes, so it cannot split a field
            # sequence or impose its own lattice settings.
            continue
        if payload.get("model") in _FIELD_MODELS:
            run.append(index)
            continue
        if run:
            _normalize_field_sequence([payloads[item] for item in run], start=run[0])
            run = []
    if run:
        _normalize_field_sequence([payloads[item] for item in run], start=run[0])
    return payloads


def _is_enabled(payload: dict[str, Any]) -> bool:
    return bool(payload.get("enabled", True))


def _normalize_field_sequence(payloads: list[dict[str, Any]], *, start: int) -> None:
    """Apply the canonical cell-set lattice policy to one field sequence."""
    configs = [payload.get("cellSet") for payload in payloads]
    if any(not isinstance(config, dict) for config in configs):
        raise ValueError(f"field sequence at step index {start} requires a cellSet object on every step")

    heights = [float(config["height"]) for config in configs]
    depths = [float(config["substrateDepth"]) for config in configs if config.get("substrateDepth") is not None]
    if not depths:
        raise ValueError(f"field sequence at step index {start} requires cellSet.substrateDepth on at least one step")
    covers = {str(config.get("coverMaterial") or "GAS") for config in configs}
    if len(covers) != 1:
        step_ids = [str(payload.get("id")) for payload in payloads]
        raise ValueError(
            f"field sequence cellSet.coverMaterial must match across steps {step_ids}: got {sorted(covers)}"
        )
    periodic = {bool(config.get("periodic", False)) for config in configs}
    if len(periodic) != 1:
        step_ids = [str(payload.get("id")) for payload in payloads]
        raise ValueError(f"field sequence cellSet.periodic must match across steps {step_ids}: got {sorted(periodic)}")

    canonical = {
        "height": max(heights),
        "substrateDepth": max(depths),
        "coverMaterial": covers.pop(),
        "periodic": periodic.pop(),
    }
    for payload, config in zip(payloads, configs):
        normalized = dict(config)
        normalized.update(canonical)
        payload["cellSet"] = normalized


def _template_project(flow: FlowRevisionV1, binding: GeometryBindingV1) -> ProjectV7:
    """Validate the geometry binding by building an empty V7 execution project.

    V7 is the single execution target for both 2D and 3D. V6 remains available
    as an explicit legacy target, but this compiler never silently downgrades.
    """

    if binding.dimension == 3:
        return ProjectV7(
            schemaVersion=7,
            name=flow.name,
            dimension=3,
            units=binding.units,
            domain=binding.domain,
            geometry=binding.geometry,
            steps=[],
            solver=flow.solver,
            view3D=View3DV3(),
            materialDefinitions=binding.materialDefinitions,
        )
    return ProjectV7(
        schemaVersion=7,
        name=flow.name,
        dimension=2,
        units=binding.units,
        domain=binding.domain,
        geometry=binding.geometry,
        steps=[],
        solver=flow.solver,
        view=View(panX=0.0, panH=0.0, zoom=1.0, showGrid=True),
        materialDefinitions=binding.materialDefinitions,
    )


def _compiled_steps(
    flow: FlowRevisionV1,
    placements: dict[str, tuple[str, str]] | None = None,
    flow_paths: dict[str, str] | None = None,
    parameter_provenance: dict[str, dict[str, ParameterProvenanceV1]] | None = None,
    variable_refs: dict[str, list[str]] | None = None,
    step_payloads: list[dict[str, Any]] | None = None,
) -> tuple[list[Any], list[SourceMapEntryV1]]:
    adapter = TypeAdapter(StepV7)
    compiled: list[Any] = []
    source_map: list[SourceMapEntryV1] = []
    placements = placements or {}
    flow_paths = flow_paths or {}
    parameter_provenance = parameter_provenance or {}
    variable_refs = variable_refs or {}
    payloads = step_payloads if step_payloads is not None else _normalized_step_payloads(flow)

    for step, source_payload in zip(flow.steps, payloads):
        module_path, iteration_key = placements.get(step.id, ("root", "i0"))
        step_id = stable_step_id(
            flow_id=flow.flowId,
            module_instance_id_path=module_path,
            authored_step_id=step.id,
            iteration_key=iteration_key,
        )
        payload = dict(source_payload)
        payload["id"] = step_id
        compiled_step = adapter.validate_python(payload)
        compiled.append(compiled_step)
        # P4-1 expansion knows whether a field came from a literal, a variable
        # or an expression. Carry that through to the SourceMap instead of
        # flattening every resolved field back to "literal"; callers that did
        # not expand a definition (P0) keep the literal-only default.
        expanded_provenance = parameter_provenance.get(step.id) or {}
        provenance: dict[str, ParameterProvenanceV1] = {}
        for key, value in payload.items():
            if key in {"id", "name", "enabled"}:
                continue
            entry = expanded_provenance.get(key)
            provenance[key] = (
                entry.model_copy(deep=True)
                if entry is not None
                else ParameterProvenanceV1(kind="literal", resolvedValue=value)
            )
        source_map.append(
            SourceMapEntryV1(
                stepId=step_id,
                flowPath=flow_paths.get(step.id, f"{module_path}/{step.id}"),
                authoredStepId=step.id,
                moduleInstanceIdPath=module_path,
                iterationKey=iteration_key,
                parameterProvenance=provenance,
                variableRefs=list(variable_refs.get(step.id, [])),
            )
        )
    return compiled, source_map


def _module_revisions_payload(
    values: list[dict[str, Any]] | list[ModuleRevisionRefV1] | None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for value in values or []:
        payload = value.model_dump(mode="json", exclude_none=True) if hasattr(value, "model_dump") else dict(value)
        entries.append({"moduleId": str(payload["moduleId"]), "revision": int(payload["revision"])})
    return sorted(entries, key=lambda entry: (entry["moduleId"], entry["revision"]))


def compile_flow(
    flow: FlowRevisionV1,
    binding: GeometryBindingV1,
    *,
    compiler_version: str = COMPILER_VERSION,
    engine_versions: dict[str, str] | None = None,
    placements: dict[str, tuple[str, str]] | None = None,
    flow_paths: dict[str, str] | None = None,
    parameter_provenance: dict[str, dict[str, ParameterProvenanceV1]] | None = None,
    variable_refs: dict[str, list[str]] | None = None,
    variable_set: list[dict[str, Any]] | None = None,
    overrides: list[dict[str, Any]] | None = None,
    module_revisions: list[dict[str, Any]] | list[ModuleRevisionRefV1] | None = None,
) -> CompiledWorkflowV1:
    """Compile a literal-step flow and a geometry binding into ProjectV7.

    Inputs are deep-copied before use. A later edit to ``flow`` or ``binding``
    must not mutate an already returned compile result or invalidate its hashes.

    ``placements`` is the P4-1 extension: it maps an authored step id to the
    module instance path and iteration key produced by module/loop expansion.
    Omitting it keeps the P0 behavior (root instance, first iteration).
    ``flow_paths`` optionally carries the authored path for module/loop steps so
    the SourceMap keeps the path from the expansion instead of guessing it from
    the flattened authored id. ``parameter_provenance``/``variable_refs`` carry
    the expansion's per-field origin into the SourceMap; omitting them keeps the
    P0 literal provenance. ``variable_set``/``overrides`` are provenance only;
    they never change step ids. ``module_revisions`` records the concrete module
    versions that produced the expanded steps; omitting it keeps the P0 compile
    identity unchanged.
    """

    flow = flow.model_copy(deep=True)
    binding = binding.model_copy(deep=True)

    template = _template_project(flow, binding)
    normalized_geometry = dict(template.geometry)
    step_payloads = _normalized_step_payloads(flow)
    steps, source_map = _compiled_steps(
        flow,
        placements,
        flow_paths,
        parameter_provenance=parameter_provenance,
        variable_refs=variable_refs,
        step_payloads=step_payloads,
    )

    if binding.dimension == 3:
        project = ProjectV7(
            schemaVersion=7,
            name=flow.name,
            dimension=3,
            units=binding.units,
            domain=binding.domain,
            geometry=normalized_geometry,
            steps=steps,
            solver=flow.solver,
            view3D=View3DV3(),
            materialDefinitions=binding.materialDefinitions,
        )
    else:
        project = ProjectV7(
            schemaVersion=7,
            name=flow.name,
            dimension=2,
            units=binding.units,
            domain=binding.domain,
            geometry=normalized_geometry,
            steps=steps,
            solver=flow.solver,
            view=View(panX=0.0, panH=0.0, zoom=1.0, showGrid=True),
            materialDefinitions=binding.materialDefinitions,
        )

    module_revisions = _module_revisions_payload(module_revisions)
    source_map_digest = source_map_hash(source_map)
    flow_payload = flow.model_dump(mode="json", exclude_none=True)
    flow_payload["steps"] = step_payloads
    if placements:
        flow_payload["placements"] = {
            authored: {"moduleInstanceIdPath": path, "iterationKey": key}
            for authored, (path, key) in sorted(placements.items())
        }
    if flow_paths:
        flow_payload["flowPaths"] = dict(sorted(flow_paths.items()))
    if module_revisions:
        flow_payload["moduleRevisions"] = module_revisions
    # P4-1 provenance changed the SourceMap encoding without changing the
    # compiled project. Give that encoding its own compile identity so a
    # definition compiled by an older server keeps its existing compileId
    # instead of colliding with a new envelope under the same id. Including the
    # SourceMap digest also keeps two provenance encodings with identical steps
    # (for example different variable names resolving to the same value) apart.
    # P0 callers pass neither argument, so their flow payload stays
    # byte-for-byte stable.
    if parameter_provenance or variable_refs:
        flow_payload["parameterProvenanceVersion"] = 1
        flow_payload["parameterProvenanceSourceMapHash"] = source_map_digest
    flow_hash = sha256_hex(flow_payload)
    binding_hash = sha256_hex(binding.model_dump(mode="json", exclude_none=True))
    material_hash = sha256_hex(
        [item.model_dump(mode="json", exclude_none=True) for item in (binding.materialDefinitions or [])]
    )
    project_hash = full_project_hash(project)
    engine_versions = dict(engine_versions or {})
    variable_set = list(variable_set or [])
    overrides = list(overrides or [])

    compile_id = compile_record_id(
        compiler_version=compiler_version,
        flow_revision_hash=flow_hash,
        geometry_binding_hash=binding_hash,
        material_definitions_hash=material_hash,
        variable_set=variable_set,
        overrides=overrides,
        seed_policy="shared-run-seed",
        step_id_policy=STEP_ID_POLICY,
        engine_versions=engine_versions,
        module_revisions=module_revisions,
    )
    record = CompileRecordV1(
        compileId=compile_id,
        compilerVersion=compiler_version,
        flowId=flow.flowId,
        flowRevisionId=f"{flow.flowId}:{flow.revision}",
        flowRevisionHash=flow_hash,
        geometryBindingId=binding.bindingId,
        geometryBindingHash=binding_hash,
        materialCatalogVersion=binding.materialCatalogVersion,
        materialDefinitionsHash=material_hash,
        defaultValuesVersion=binding.defaultValuesVersion,
        variableSet=variable_set,
        overrides=overrides,
        moduleRevisions=module_revisions or None,
        engineVersions=engine_versions,
        stepIdPolicy=STEP_ID_POLICY,
        fullProjectHash=project_hash,
        sourceMapHash=source_map_digest,
    )
    return CompiledWorkflowV1(project=project, sourceMap=source_map, compileRecord=record)
