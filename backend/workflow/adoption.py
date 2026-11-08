"""Adopt an existing ProjectV7 into the workflow layer and build a first Route."""

from __future__ import annotations

import uuid
from typing import Any

from ..models import ProjectV7, SolverV7, parse_project
from ..validation import ProjectInputError, validate_project
from .compiler import compile_flow
from .hashing import full_project_hash
from .ids import sha256_hex
from .models import FlowRevisionV1, GeometryBindingV1
from .routes import AttemptV1, RoutePlanV1, RouteV1, SegmentV1


def adopt_project(
    project_payload: dict[str, Any],
    *,
    flow_id: str | None = None,
    steps: list[Any] | None = None,
    geometry_source: dict[str, Any] | None = None,
    revision: int = 1,
    placements: dict[str, tuple[str, str]] | None = None,
    flow_paths: dict[str, str] | None = None,
    parameter_provenance: dict[str, dict[str, Any]] | None = None,
    variable_refs: dict[str, list[str]] | None = None,
    variable_set: list[dict[str, Any]] | None = None,
    overrides: list[dict[str, Any]] | None = None,
    module_revisions: list[dict[str, Any]] | None = None,
) -> tuple[FlowRevisionV1, GeometryBindingV1, Any]:
    try:
        project = parse_project(project_payload)
        validated, _warnings = validate_project(project_payload, kind="simulate")
    except (ValueError, ProjectInputError) as exc:
        raise ValueError(f"project cannot be adopted: {exc}") from exc
    if not isinstance(validated, ProjectV7):
        raise ValueError("workflow adoption currently requires a schemaVersion 7 project")
    suffix = uuid.uuid4().hex[:12]
    flow_id = flow_id or f"flow_{suffix}"
    material_definitions = [
        item.model_dump(mode="json", exclude_none=True)
        for item in (validated.materialDefinitions or [])
    ]
    binding_payload = {
        "schemaVersion": 1,
        "name": f"{validated.name} · geometry",
        "dimension": validated.dimension,
        "units": validated.units.model_dump(mode="json"),
        "domain": validated.domain.model_dump(mode="json", exclude_none=True),
        "geometry": validated.geometry,
        "geometrySource": geometry_source,
        "materialDefinitions": material_definitions,
        "materialCatalogVersion": "4.6.2",
        "defaultValuesVersion": "1",
        "schemaVersions": {},
    }
    binding = GeometryBindingV1(
        schemaVersion=1,
        bindingId=f"bind_{sha256_hex(binding_payload)[:16]}",
        name=binding_payload["name"],
        dimension=validated.dimension,
        units=validated.units,
        domain=validated.domain,
        geometry=validated.geometry,
        geometrySource=geometry_source,
        materialDefinitions=validated.materialDefinitions,
    )
    flow = FlowRevisionV1(
        schemaVersion=1,
        flowId=flow_id,
        revision=revision,
        name=validated.name,
        steps=steps if steps is not None else validated.steps,
        solver=validated.solver,
    )
    compiled = compile_flow(
        flow,
        binding,
        placements=placements,
        flow_paths=flow_paths,
        parameter_provenance=parameter_provenance,
        variable_refs=variable_refs,
        variable_set=variable_set,
        overrides=overrides,
        module_revisions=module_revisions,
    )
    return flow, binding, compiled


def first_route(compiled: Any, *, name: str | None = None, split_after_step_id: str | None = None) -> RoutePlanV1:
    suffix = uuid.uuid4().hex[:10]
    step_ids = [step.id for step in compiled.project.steps if step.enabled]
    if not step_ids:
        raise ValueError("workflow route requires at least one enabled step")
    attempt = AttemptV1(
        attemptId=f"attempt_{suffix}",
        routeId=f"route_{suffix}",
        compileId=compiled.compileRecord.compileId,
        submissionKey=f"submit_{suffix}",
        startAfterStepId=None,
        throughStepId=step_ids[-1],
        seedPolicy="shared-run-seed",
        runSeed=compiled.project.solver.random.seed if compiled.project.solver.random.mode == "fixed" else 0,
        flowDigest=compiled.compileRecord.fullProjectHash,
        status="planned",
    )
    splits: list[SegmentV1] = []
    range_specs = [(None, step_ids[-1])]
    if split_after_step_id and split_after_step_id in step_ids and step_ids.index(split_after_step_id) < len(step_ids) - 1:
        range_specs = [(None, split_after_step_id), (split_after_step_id, step_ids[-1])]
    for index, (start_after, through) in enumerate(range_specs):
        splits.append(SegmentV1(
            segmentId=f"segment_{suffix}_{index}",
            attemptId=attempt.attemptId,
            sourceCompileId=compiled.compileRecord.compileId,
            segmentProjectHash=full_project_hash(compiled.project),
            startAfterStepId=start_after,
            throughStepId=through,
            startCheckpointRef=None,
            endCheckpointRef=None,
            runId=f"pending_{suffix}_{index}",
            provenanceInputHash="0" * 64,
            flowDigest=compiled.compileRecord.fullProjectHash,
            executionHash="0" * 64,
            resultDigest=None,
            status="planned",
        ))
    return RoutePlanV1(
        route=RouteV1(
            routeId=attempt.routeId,
            flowId=compiled.compileRecord.flowId,
            name=name or f"{compiled.project.name} · route",
            headAttemptId=attempt.attemptId,
        ),
        stepOrder=step_ids,
        attempts=[attempt],
        segments=splits,
        reuse=[],
    )
