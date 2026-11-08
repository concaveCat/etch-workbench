"""P4-2 measurement-driven branches and replayable decision records.

The native engine still executes one bounded Segment per run. P4-2 composes a
*workflow run* out of existing pieces: a succeeded prefix Segment is measured,
the measurement selects a branch, and a new Attempt/Segment reuses the verified
prefix checkpoint with a recompiled suffix. Every step of that composition is
recorded on :class:`MeasurementDecisionV1`, so replay can follow the record
without measuring or deciding again.

A branch may stop before the end of the route (``branchThroughStepId``). That
makes the next decision measurable at the branch's end checkpoint, so several
measurement-driven branches can form one sequence under one ``workflowRunId``.
"""

from __future__ import annotations

import hmac
import json
import math
import operator
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, ValidationError

from ..models import StepV7, StrictModel
from .branches import create_branch
from .compiler import compile_flow
from .experiments import authored_project
from .ids import sha256_hex
from .models import CompiledWorkflowV1, FlowRevisionV1, ParameterProvenanceV1
from .routes import AttemptV1, RoutePlanV1, SegmentV1
from .store import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowStore,
    WorkflowValidation,
)
from .verification import prefix_compatibility


DecisionOperator = Literal["lt", "lte", "gt", "gte", "eq"]
ChosenBranch = Literal["true", "false"]
MetricUnit = Literal["um", "nm"]
EquivalenceStatus = Literal["not_evaluated", "established", "failed", "inconclusive"]

_OPERATORS = {
    "lt": operator.lt,
    "lte": operator.le,
    "gt": operator.gt,
    "gte": operator.ge,
    "eq": operator.eq,
}

DecisionId = Annotated[str, Field(min_length=4, max_length=64, pattern=r"^dec_[A-Za-z0-9_-]+$")]
WorkflowRunId = Annotated[str, Field(min_length=8, max_length=64, pattern=r"^wfr_[A-Za-z0-9_-]+$")]
Hash64 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class BranchParametersV1(StrictModel):
    """Authored step id -> field overrides applied after the reused prefix."""

    parameters: dict[str, dict[str, Any]]


class MeasurementBranchesV1(StrictModel):
    true: BranchParametersV1
    false: BranchParametersV1


class MeasurementDecisionRequestV1(StrictModel):
    baseCompileId: str | None = None
    startAfterStepId: str | None = None
    # Optional end of the new branch Segment. Defaults to the last route step;
    # a shorter branch leaves a checkpoint for the next measurement decision.
    branchThroughStepId: str | None = None
    recipeId: Annotated[str, Field(min_length=1, max_length=64)]
    recipeVersion: Annotated[int, Field(ge=1)]
    operator: DecisionOperator
    threshold: float
    branches: MeasurementBranchesV1
    # Optional continuation of an existing decision sequence on the same route.
    workflowRunId: WorkflowRunId | None = None


class MeasurementDecisionV1(StrictModel):
    """Immutable record of one measurement-driven branch decision.

    ``replayOf`` turns a replay into another explicit record: it keeps the
    original measurement, chosen branch and resolved parameters, but points at
    the newly created Attempt/Segment.
    """

    schemaVersion: Literal[1] = 1
    decisionId: DecisionId
    workflowRunId: WorkflowRunId
    routeId: Annotated[str, Field(min_length=3, max_length=64)]
    sourceSegmentId: Annotated[str, Field(min_length=3, max_length=64)]
    sourceAttemptId: Annotated[str, Field(min_length=3, max_length=64)] | None = None
    sourceCompileId: Annotated[str, Field(min_length=3, max_length=64)] | None = None
    sourceCompileHash: Hash64 | None = None
    sourceRunId: Annotated[str, Field(min_length=3, max_length=64)]
    startAfterStepId: Annotated[str, Field(min_length=3, max_length=64)] | None = None
    sourceCheckpointHash: Hash64 | None = None
    sourcePrefixDigest: Hash64 | None = None
    seed: Annotated[int, Field(ge=0, le=4294967295)] | None = None
    measurementId: Annotated[str, Field(min_length=3, max_length=64)]
    measurementStatus: str = "measured"
    measurementBindingStatus: Literal["unbound", "verified", "failed"] = "unbound"
    measurementEquivalenceStatus: EquivalenceStatus = "not_evaluated"
    recipeId: Annotated[str, Field(min_length=1, max_length=64)]
    recipeVersion: Annotated[int, Field(ge=1)]
    metricId: Annotated[str, Field(min_length=1, max_length=64)]
    method: Literal["cd_at_height", "etch_depth"] | None = None
    measuredValue: float
    unit: MetricUnit
    operator: DecisionOperator
    threshold: float
    result: bool
    chosenBranch: ChosenBranch
    # Both candidate parameter maps, so replay can prove that the chosen branch
    # is the recorded one instead of trusting a single mutable field.
    candidateParameters: MeasurementBranchesV1 | None = None
    resolvedParameters: dict[str, dict[str, Any]]
    targetCompileId: Annotated[str, Field(pattern=r"^c_[a-f0-9]{24}$")]
    targetCompileHash: Hash64 | None = None
    branchAttemptId: Annotated[str, Field(min_length=3, max_length=64)]
    branchSegmentId: Annotated[str, Field(min_length=3, max_length=64)]
    branchThroughStepId: Annotated[str, Field(min_length=3, max_length=64)] | None = None
    replayOf: DecisionId | None = None
    createdAt: str
    # Tamper-evident digest over every other field. It is computed when the
    # record is created and re-checked on read/replay. Old rows written before
    # this field existed have ``None`` and are explicitly refused for replay.
    recordHash: Hash64 | None = None


def decision_record_hash(decision: MeasurementDecisionV1) -> str:
    """Canonical digest of a decision record, excluding ``recordHash`` itself."""

    payload = decision.model_dump(mode="json", exclude_none=True)
    payload.pop("recordHash", None)
    return sha256_hex(payload)


def seal_measurement_decision(decision: MeasurementDecisionV1) -> MeasurementDecisionV1:
    """Return a copy carrying the digest for its current field values."""

    return decision.model_copy(update={"recordHash": decision_record_hash(decision)})


def verify_measurement_decision(decision: MeasurementDecisionV1) -> MeasurementDecisionV1:
    """Reject a missing or mismatched record digest with a 409-level conflict."""

    if decision.recordHash is None:
        raise WorkflowConflict(
            f"decision {decision.decisionId!r} predates tamper-evident records and "
            "has no recordHash; recreate it with the current server before replay"
        )
    expected = decision_record_hash(decision)
    if not hmac.compare_digest(expected, decision.recordHash):
        raise WorkflowConflict(
            f"decision {decision.decisionId!r} recordHash does not match its content; "
            "the stored decision was modified outside the workflow record"
        )
    return decision


class WorkflowRunSummaryV1(StrictModel):
    workflowRunId: WorkflowRunId
    routeId: Annotated[str, Field(min_length=3, max_length=64)]
    decisionCount: Annotated[int, Field(ge=1)]
    replayCount: Annotated[int, Field(ge=0)]
    decisionIds: list[DecisionId]
    branchSegmentIds: list[str]
    firstDecisionAt: str
    lastDecisionAt: str


class WorkflowRunListResponse(StrictModel):
    runs: list[WorkflowRunSummaryV1]


class WorkflowRunDetailV1(StrictModel):
    workflowRunId: WorkflowRunId
    routeId: Annotated[str, Field(min_length=3, max_length=64)]
    decisions: list[MeasurementDecisionV1]
    attempts: list[AttemptV1]
    segments: list[SegmentV1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_decision_id() -> str:
    return f"dec_{uuid.uuid4().hex[:16]}"


def _new_workflow_run_id() -> str:
    return f"wfr_{uuid.uuid4().hex[:16]}"


def evaluate(operator_name: str, measured: float, threshold: float) -> bool:
    try:
        compare = _OPERATORS[operator_name]
    except KeyError as exc:
        raise WorkflowValidation(f"unsupported decision operator {operator_name!r}") from exc
    return bool(compare(measured, threshold))


def _workflow_run_id(
    store: WorkflowStore, route_id: str, requested: str | None
) -> str:
    """Continue the route's decision sequence unless a new run is requested.

    The first decision creates ``wfr_...``. Later decisions on the same route
    share it so one workflow run can contain a chain of measurement-driven
    branches; passing ``workflowRunId`` explicitly continues that sequence or
    starts a new run. A run id may never be shared between two routes.
    """

    if requested is None:
        decisions = store.list_measurement_decisions(route_id)
        return decisions[-1].workflowRunId if decisions else _new_workflow_run_id()
    other_routes = store.workflow_run_route_ids(requested) - {route_id}
    if other_routes:
        raise WorkflowConflict(
            f"workflow run {requested!r} already belongs to route {sorted(other_routes)[0]!r}"
        )
    return requested


def _source_segment(plan: RoutePlanV1, start_after_step_id: str | None) -> SegmentV1:
    succeeded = [
        segment
        for segment in plan.segments
        if segment.status == "succeeded"
        and segment.endCheckpointRef is not None
        and not segment.runId.startswith("pending_")
    ]
    if start_after_step_id is None:
        if not succeeded:
            raise WorkflowNotFound(
                "route has no succeeded prefix segment with a checkpoint to measure"
            )
        last_step = plan.stepOrder[-1]
        candidates = [
            segment for segment in succeeded if segment.throughStepId != last_step
        ]
        if not candidates:
            raise WorkflowConflict(
                "route has no succeeded prefix with a suffix to branch; "
                "the latest succeeded segment already reaches the final step"
            )
        head = plan.route.headAttemptId
        head_candidates = [
            segment for segment in candidates if segment.attemptId == head
        ]
        return (head_candidates or candidates)[-1]
    source = next(
        (
            segment for segment in reversed(succeeded)
            if segment.throughStepId == start_after_step_id
        ),
        None,
    )
    if source is None:
        raise WorkflowNotFound(
            f"route has no succeeded segment through {start_after_step_id!r}"
        )
    return source


def _flow_revision(compiled: CompiledWorkflowV1) -> int:
    try:
        return int(compiled.compileRecord.flowRevisionId.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 1


def _compile_provenance(
    source_compiled: CompiledWorkflowV1,
    resolved: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Carry the source compile's provenance and append the decision overrides.

    Dropping the source ``variableSet``/``overrides`` would make two distinct
    base compiles (for example a variable-driven flow and an equivalent literal
    flow) collapse into the same target compile id. Chained decisions also need
    the earlier decision's entries to stay auditable. Entries that the new
    decision replaces are dropped so each (step, parameter) has one value.
    """

    record = source_compiled.compileRecord
    replaced = {
        (authored_step_id, field)
        for authored_step_id, fields in resolved.items()
        for field in fields
    }
    overrides: list[dict[str, Any]] = []
    variable_set: list[dict[str, Any]] = []
    for entry in record.overrides:
        key = (entry.get("stepId"), entry.get("parameter"))
        if key[0] is not None and key[1] is not None and key in replaced:
            continue
        overrides.append(dict(entry))
    replaced_names = {f"{step_id}.{field}" for step_id, field in replaced}
    for entry in record.variableSet:
        if entry.get("name") in replaced_names:
            continue
        variable_set.append(dict(entry))
    for authored_step_id in sorted(resolved):
        fields = resolved[authored_step_id]
        for field in sorted(fields):
            value = fields[field]
            overrides.append({
                "stepId": authored_step_id,
                "parameter": field,
                "value": value,
                "source": "measurement-decision",
            })
            variable_set.append({
                "name": f"{authored_step_id}.{field}",
                "value": value,
                "source": "measurement-decision",
            })
    return overrides, variable_set


def _build_target_compile(
    store: WorkflowStore,
    source_compiled: CompiledWorkflowV1,
    resolved: dict[str, dict[str, Any]],
) -> CompiledWorkflowV1:
    """Rebuild the authored flow, apply resolved overrides and compile again.

    The source compile's identity inputs (compiler version, engine versions,
    placements, module revisions, variables and overrides) are reused so replay
    reproduces the same target id on a later server build.
    """

    record = source_compiled.compileRecord
    binding = store.get_binding(record.geometryBindingId)
    payload = authored_project(source_compiled)
    steps_by_id = {str(step.get("id")): step for step in payload.get("steps", [])}
    adapter = TypeAdapter(StepV7)
    for authored_step_id, fields in resolved.items():
        step = steps_by_id.get(authored_step_id)
        if step is None:
            raise WorkflowValidation(
                f"resolved parameter step {authored_step_id!r} is not an authored step "
                "of the source compile"
            )
        if "id" in fields:
            raise WorkflowValidation(
                "resolved parameters must not change an authored step id"
            )
        try:
            merged = json.loads(json.dumps(step))
            merged.update(fields)
            merged["id"] = authored_step_id
            adapter.validate_python(merged)
        except (ValueError, TypeError, ValidationError) as exc:
            raise WorkflowValidation(
                f"resolved parameters for authored step {authored_step_id!r} are invalid: {exc}"
            ) from exc
        step.update(fields)

    try:
        steps = [adapter.validate_python(step) for step in payload["steps"]]
        flow = FlowRevisionV1(
            schemaVersion=1,
            flowId=record.flowId,
            revision=_flow_revision(source_compiled),
            name=source_compiled.project.name,
            steps=steps,
            solver=source_compiled.project.solver.model_copy(deep=True),
        )
    except (ValueError, ValidationError) as exc:
        raise WorkflowValidation(f"decision target flow is invalid: {exc}") from exc

    placements: dict[str, tuple[str, str]] | None = None
    flow_paths: dict[str, str] | None = None
    if record.moduleRevisions or any(
        (entry.moduleInstanceIdPath, entry.iterationKey) != ("root", "i0")
        for entry in source_compiled.sourceMap
    ):
        placements = {
            entry.authoredStepId: (entry.moduleInstanceIdPath, entry.iterationKey)
            for entry in source_compiled.sourceMap
        }
    if any(entry.flowPath != f"root/{entry.authoredStepId}" for entry in source_compiled.sourceMap):
        flow_paths = {entry.authoredStepId: entry.flowPath for entry in source_compiled.sourceMap}

    overrides, variable_set = _compile_provenance(source_compiled, resolved)
    # Keep the source expansion/variable provenance and replace only the fields
    # this decision resolved. Without this, a decision target compile would
    # flatten every inherited field back to "literal" and lose the variable
    # references that flow diff and rerun scope depend on.
    parameter_provenance = {
        entry.authoredStepId: {
            field: provenance.model_copy(deep=True)
            for field, provenance in entry.parameterProvenance.items()
        }
        for entry in source_compiled.sourceMap
    }
    step_variable_refs = {
        entry.authoredStepId: list(entry.variableRefs)
        for entry in source_compiled.sourceMap
    }
    for authored_step_id, fields in resolved.items():
        provenance = parameter_provenance.setdefault(authored_step_id, {})
        for field, value in fields.items():
            provenance[field] = ParameterProvenanceV1(
                kind="variable",
                source="measurement-decision",
                resolvedValue=value,
            )
    module_revisions = (
        [item.model_dump(mode="json", exclude_none=True) for item in record.moduleRevisions]
        if record.moduleRevisions
        else None
    )
    return compile_flow(
        flow,
        binding,
        compiler_version=record.compilerVersion,
        engine_versions=dict(record.engineVersions),
        placements=placements,
        flow_paths=flow_paths,
        parameter_provenance=parameter_provenance,
        variable_refs=step_variable_refs,
        variable_set=variable_set,
        overrides=overrides,
        module_revisions=module_revisions,
    )


def _register_target(store: WorkflowStore, compiled: CompiledWorkflowV1) -> None:
    store.register_binding(store.get_binding(compiled.compileRecord.geometryBindingId))
    store.register_compile(compiled)


def _compile_step_ids(
    source_compiled: CompiledWorkflowV1,
) -> dict[str, str]:
    return {entry.authoredStepId: entry.stepId for entry in source_compiled.sourceMap}


def _resolved_scope(
    source_compiled: CompiledWorkflowV1,
    start_after_step_id: str,
    resolved: dict[str, dict[str, Any]],
    *,
    through_step_id: str | None = None,
) -> list[str]:
    """Validate authored overrides and return the changed compiled step ids.

    A decision may only change the suffix after the reused prefix; changing a
    prefix step would silently invalidate the checkpoint reuse contract. When
    the branch stops before the end of the route, only steps up to its end may
    change.
    """

    compiled_ids = [step.id for step in source_compiled.project.steps]
    if start_after_step_id not in compiled_ids:
        raise WorkflowConflict(
            f"startAfterStepId {start_after_step_id!r} is not a compiled step of the route"
        )
    start_index = compiled_ids.index(start_after_step_id)
    end_index = len(compiled_ids) - 1
    if through_step_id is not None:
        if through_step_id not in compiled_ids:
            raise WorkflowConflict(
                f"branchThroughStepId {through_step_id!r} is not a compiled step of the route"
            )
        end_index = compiled_ids.index(through_step_id)
        if end_index <= start_index:
            raise WorkflowValidation(
                f"branchThroughStepId {through_step_id!r} leaves no suffix to branch"
            )
    elif start_index >= end_index:
        raise WorkflowValidation(
            f"startAfterStepId {start_after_step_id!r} leaves no suffix to branch"
        )
    authored_to_compiled = _compile_step_ids(source_compiled)
    changed: list[str] = []
    for authored_step_id in resolved:
        if authored_step_id not in authored_to_compiled:
            raise WorkflowValidation(
                f"resolved parameter step {authored_step_id!r} is not an authored step "
                "of the source compile"
            )
        compiled_step_id = authored_to_compiled[authored_step_id]
        index = compiled_ids.index(compiled_step_id)
        if index <= start_index:
            raise WorkflowValidation(
                f"resolved parameter step {authored_step_id!r} is inside the reused prefix"
            )
        if index > end_index:
            raise WorkflowValidation(
                f"resolved parameter step {authored_step_id!r} is after the branch end "
                f"{through_step_id!r}"
            )
        changed.append(compiled_step_id)
    return changed


def _branch_through(
    plan: RoutePlanV1, start_after_step_id: str, requested: str | None
) -> str:
    step_order = plan.stepOrder
    start_index = step_order.index(start_after_step_id)
    through = requested or step_order[-1]
    if through not in step_order:
        raise WorkflowValidation(
            f"branchThroughStepId {through!r} is not a route step"
        )
    if step_order.index(through) <= start_index:
        raise WorkflowValidation(
            f"branchThroughStepId {through!r} must come after {start_after_step_id!r}"
        )
    return through


def _source_prefix(
    source_compiled: CompiledWorkflowV1, start_after_step_id: str, seed: int
) -> str:
    step_ids = [step.id for step in source_compiled.project.steps]
    try:
        index = step_ids.index(start_after_step_id)
        return prefix_compatibility(
            source_compiled, step_ids[: index + 1], seed
        ).prefixDigest
    except ValueError as exc:
        raise WorkflowConflict(
            f"reused prefix identity cannot be derived: {exc}"
        ) from exc


def _branch_ids(plan: RoutePlanV1, submission_key: str) -> tuple[str, str]:
    attempt = next(
        (item for item in plan.attempts if item.submissionKey == submission_key),
        None,
    )
    if attempt is None:
        raise WorkflowConflict("measurement decision branch attempt was not persisted")
    segment = next(
        (item for item in plan.segments if item.attemptId == attempt.attemptId),
        None,
    )
    if segment is None:
        raise WorkflowConflict("measurement decision branch segment was not persisted")
    return attempt.attemptId, segment.segmentId


def _measure_source(
    store: WorkflowStore,
    source: SegmentV1,
    recipe_id: str,
    recipe_version: int,
):
    recipe = store.get_recipe(recipe_id, recipe_version)
    metric = store.get_metric(recipe.metricId)
    measurement = store.measure_run(
        recipe, metric, source.runId, source.throughStepId
    )
    if (
        measurement.status != "measured"
        or not isinstance(measurement.value, (int, float))
        or not math.isfinite(float(measurement.value))
    ):
        raise WorkflowValidation(
            f"measurement {measurement.measurementId} is not usable for a decision: "
            f"{measurement.status} ({measurement.reason or 'no reason'})"
        )
    try:
        measurement = store.bind_measurement(
            measurement.measurementId, source.segmentId, source.runId, source.throughStepId
        )
    except (WorkflowConflict, WorkflowNotFound, ValueError) as exc:
        raise WorkflowConflict(
            f"measurement {measurement.measurementId} could not be bound to the "
            f"source segment: {exc}"
        ) from exc
    return recipe, metric, measurement


def _binding_reasons(measurement: Any) -> list[str]:
    evidence = measurement.bindingEvidence
    if evidence is None:
        return []
    reasons: list[str] = []
    for section in ("execution", "targetStep", "sourceIdentity"):
        reasons.extend(getattr(evidence, section).reasons)
    return reasons


def _require_verified(measurement: Any) -> None:
    """A branch decision needs a measurement tied to the real prefix run."""

    if measurement.bindingStatus == "verified":
        return
    reasons = _binding_reasons(measurement)
    detail = f": {'; '.join(reasons)}" if reasons else ""
    raise WorkflowConflict(
        f"measurement {measurement.measurementId} is not bound to the source segment "
        f"(bindingStatus={measurement.bindingStatus}){detail}"
    )


def create_measurement_decision(
    store: WorkflowStore,
    route_id: str,
    request: MeasurementDecisionRequestV1 | dict[str, Any],
) -> dict[str, Any]:
    """Measure the succeeded prefix, pick a branch and append it to the route."""

    if not isinstance(request, MeasurementDecisionRequestV1):
        request = MeasurementDecisionRequestV1.model_validate(request)
    if not math.isfinite(request.threshold):
        raise WorkflowValidation("decision threshold must be finite")
    # Resolve the run identity before any side effect so a cross-route run id
    # cannot leave a Measurement or a branch Attempt behind.
    workflow_run_id = _workflow_run_id(store, route_id, request.workflowRunId)
    plan = store.get_plan(route_id)
    source = _source_segment(plan, request.startAfterStepId)
    start_after = source.throughStepId
    if source.runId.startswith("pending_") or source.endCheckpointRef is None:
        raise WorkflowConflict("source segment has no completed run checkpoint")

    source_compiled = store.get_compile(source.sourceCompileId)
    if source_compiled.compileRecord.flowId != plan.route.flowId:
        raise WorkflowConflict("source compile belongs to a different flow than the route")
    if request.baseCompileId is not None and request.baseCompileId != source.sourceCompileId:
        raise WorkflowValidation(
            "baseCompileId does not match the source segment's compile"
        )
    through = _branch_through(plan, start_after, request.branchThroughStepId)

    # Validate both candidate branches before measuring so an invalid request
    # leaves no stray Measurement behind.
    prepared: dict[str, tuple[dict[str, dict[str, Any]], CompiledWorkflowV1, list[str]]] = {}
    for name in ("true", "false"):
        resolved = getattr(request.branches, name).parameters
        changed = _resolved_scope(
            source_compiled, start_after, resolved, through_step_id=through
        )
        target = _build_target_compile(store, source_compiled, resolved)
        prepared[name] = (resolved, target, changed)

    recipe, metric, measurement = _measure_source(
        store, source, request.recipeId, request.recipeVersion
    )
    _require_verified(measurement)
    measured = float(measurement.value)  # type: ignore[arg-type]
    result = evaluate(request.operator, measured, request.threshold)
    chosen: ChosenBranch = "true" if result else "false"
    resolved, target, changed_step_ids = prepared[chosen]
    _register_target(store, target)

    source_attempt = store.get_attempt(source.attemptId)
    suffix = uuid.uuid4().hex[:10]
    submission_key = f"decision_{suffix}"
    branch_plan = create_branch(
        store,
        route_id,
        target.compileRecord.compileId,
        start_after,
        changed_step_ids,
        through_step_id=through,
        source_segment_id=source.segmentId,
        submission_key=submission_key,
    )
    attempt_id, segment_id = _branch_ids(branch_plan, submission_key)

    decision = MeasurementDecisionV1(
        decisionId=_new_decision_id(),
        workflowRunId=workflow_run_id,
        routeId=route_id,
        sourceSegmentId=source.segmentId,
        sourceAttemptId=source.attemptId,
        sourceCompileId=source.sourceCompileId,
        sourceCompileHash=source_compiled.compileRecord.fullProjectHash,
        sourceRunId=source.runId,
        startAfterStepId=start_after,
        sourceCheckpointHash=source.endCheckpointRef.stateHash,
        sourcePrefixDigest=_source_prefix(
            source_compiled, start_after, source_attempt.runSeed
        ),
        seed=source_attempt.runSeed,
        measurementId=measurement.measurementId,
        measurementStatus=measurement.status,
        measurementBindingStatus=measurement.bindingStatus,
        measurementEquivalenceStatus=measurement.equivalenceStatus,
        recipeId=recipe.recipeId,
        recipeVersion=recipe.version,
        metricId=metric.metricId,
        method=recipe.method,
        measuredValue=measured,
        unit=measurement.unit,
        operator=request.operator,
        threshold=request.threshold,
        result=result,
        chosenBranch=chosen,
        candidateParameters=request.branches,
        resolvedParameters=resolved,
        targetCompileId=target.compileRecord.compileId,
        targetCompileHash=target.compileRecord.fullProjectHash,
        branchAttemptId=attempt_id,
        branchSegmentId=segment_id,
        branchThroughStepId=through,
        createdAt=_now(),
    )
    stored = store.save_measurement_decision(seal_measurement_decision(decision))
    return {
        "decision": stored,
        "plan": branch_plan,
        "workflowRunId": stored.workflowRunId,
        "measurement": measurement,
        "resolvedParameters": stored.resolvedParameters,
    }


def _recorded_decision_checks(original: MeasurementDecisionV1) -> None:
    """Prove that a stored decision could have been produced by its own rule."""

    if not math.isfinite(original.measuredValue) or not math.isfinite(original.threshold):
        raise WorkflowConflict(
            f"decision {original.decisionId!r} has a non-finite measured value or threshold"
        )
    expected = evaluate(original.operator, original.measuredValue, original.threshold)
    if expected != original.result:
        raise WorkflowConflict(
            f"decision {original.decisionId!r} result does not match its operator, "
            "measured value and threshold"
        )
    expected_branch: ChosenBranch = "true" if expected else "false"
    if original.chosenBranch != expected_branch:
        raise WorkflowConflict(
            f"decision {original.decisionId!r} chosen branch does not match its result"
        )
    if original.candidateParameters is not None:
        candidate = getattr(
            original.candidateParameters, original.chosenBranch
        ).parameters
        if candidate != original.resolvedParameters:
            raise WorkflowConflict(
                f"decision {original.decisionId!r} resolved parameters are not the "
                f"{original.chosenBranch} branch parameters"
            )
    if original.measurementBindingStatus != "verified":
        raise WorkflowConflict(
            f"decision {original.decisionId!r} was recorded with an unverified measurement "
            f"({original.measurementBindingStatus})"
        )


def _recorded_source_checks(
    original: MeasurementDecisionV1,
    plan: RoutePlanV1,
    source: SegmentV1,
    source_compiled: CompiledWorkflowV1,
    start_after_step_id: str,
) -> None:
    if source.attemptId not in {attempt.attemptId for attempt in plan.attempts}:
        raise WorkflowConflict(
            f"decision {original.decisionId!r} source attempt is no longer in the route"
        )
    if original.sourceAttemptId is not None and source.attemptId != original.sourceAttemptId:
        raise WorkflowConflict(
            f"decision {original.decisionId!r} source attempt identity changed"
        )
    if (
        original.sourceCompileHash is not None
        and source_compiled.compileRecord.fullProjectHash != original.sourceCompileHash
    ):
        raise WorkflowConflict(
            f"decision {original.decisionId!r} source compile content changed"
        )
    if (
        original.sourceCheckpointHash is not None
        and source.endCheckpointRef is not None
        and source.endCheckpointRef.stateHash != original.sourceCheckpointHash
    ):
        raise WorkflowConflict(
            f"decision {original.decisionId!r} source checkpoint identity changed"
        )
    source_attempt = _attempt(plan, source.attemptId)
    if original.seed is not None and source_attempt.runSeed != original.seed:
        raise WorkflowConflict(
            f"decision {original.decisionId!r} source run seed changed"
        )
    if original.sourcePrefixDigest is not None:
        derived = _source_prefix(
            source_compiled, start_after_step_id, source_attempt.runSeed
        )
        if derived != original.sourcePrefixDigest:
            raise WorkflowConflict(
                f"decision {original.decisionId!r} reused prefix identity changed"
            )


def _attempt(plan: RoutePlanV1, attempt_id: str) -> AttemptV1:
    attempt = next(
        (item for item in plan.attempts if item.attemptId == attempt_id), None
    )
    if attempt is None:
        raise WorkflowConflict(f"attempt {attempt_id!r} is not in the route")
    return attempt


def _recorded_measurement_checks(
    original: MeasurementDecisionV1, measurement: Any
) -> None:
    if (
        measurement.sourceRunId != original.sourceRunId
        or measurement.sourceStepId != original.startAfterStepId
        or measurement.measurementId != original.measurementId
    ):
        raise WorkflowConflict(
            f"decision {original.decisionId!r} measurement identity no longer matches "
            "its source prefix"
        )
    if (
        measurement.status != original.measurementStatus
        or measurement.recipeId != original.recipeId
        or measurement.recipeVersion != original.recipeVersion
    ):
        raise WorkflowConflict(
            f"decision {original.decisionId!r} measurement status or recipe changed"
        )
    if measurement.value is None or float(measurement.value) != original.measuredValue:
        raise WorkflowConflict(
            f"decision {original.decisionId!r} measured value changed; replay would not "
            "follow the recorded decision"
        )
    if measurement.unit != original.unit:
        raise WorkflowConflict(
            f"decision {original.decisionId!r} measurement unit changed"
        )
    if measurement.bindingStatus != "verified":
        raise WorkflowConflict(
            f"decision {original.decisionId!r} measurement is no longer bound "
            f"(bindingStatus={measurement.bindingStatus})"
        )


def replay_measurement_decision(
    store: WorkflowStore,
    route_id: str,
    decision_id: str,
) -> dict[str, Any]:
    """Replay a recorded branch without measuring or re-deciding."""

    original = store.get_measurement_decision(
        decision_id, route_id=route_id, verify=False
    )
    if original.routeId != route_id:
        raise WorkflowNotFound(
            f"decision {decision_id!r} does not belong to route {route_id!r}"
        )
    _recorded_decision_checks(original)
    verify_measurement_decision(original)
    plan = store.get_plan(route_id)
    source = next(
        (segment for segment in plan.segments if segment.segmentId == original.sourceSegmentId),
        None,
    )
    if source is None:
        raise WorkflowNotFound(
            f"decision {decision_id!r} source segment is no longer in the route"
        )
    if source.status != "succeeded" or source.endCheckpointRef is None:
        raise WorkflowConflict(
            f"decision {decision_id!r} source segment is no longer a succeeded prefix"
        )
    if (
        (original.sourceCompileId is not None and source.sourceCompileId != original.sourceCompileId)
        or source.runId != original.sourceRunId
        or (original.startAfterStepId is not None and source.throughStepId != original.startAfterStepId)
    ):
        raise WorkflowConflict(
            f"decision {decision_id!r} source prefix identity changed"
        )
    source_compile_id = original.sourceCompileId or source.sourceCompileId
    start_after_step_id = original.startAfterStepId or source.throughStepId
    source_compiled = store.get_compile(source_compile_id)
    _recorded_source_checks(
        original, plan, source, source_compiled, start_after_step_id
    )
    through = _branch_through(plan, start_after_step_id, original.branchThroughStepId)
    target = _build_target_compile(store, source_compiled, original.resolvedParameters)
    if target.compileRecord.compileId != original.targetCompileId:
        raise WorkflowConflict(
            "decision target compile cannot be reproduced from its resolved parameters"
        )
    if (
        original.targetCompileHash is not None
        and target.compileRecord.fullProjectHash != original.targetCompileHash
    ):
        raise WorkflowConflict(
            f"decision {decision_id!r} target compile content changed"
        )
    changed_step_ids = _resolved_scope(
        source_compiled,
        start_after_step_id,
        original.resolvedParameters,
        through_step_id=through,
    )

    measurement = store.get_measurement(original.measurementId)
    _recorded_measurement_checks(original, measurement)

    # Register the rebuilt target compile only after every record check passed,
    # so a tampered replay cannot leave a compile behind.
    _register_target(store, target)

    suffix = uuid.uuid4().hex[:10]
    submission_key = f"decision_{suffix}"
    branch_plan = create_branch(
        store,
        route_id,
        target.compileRecord.compileId,
        start_after_step_id,
        changed_step_ids,
        through_step_id=through,
        source_segment_id=source.segmentId,
        submission_key=submission_key,
    )
    attempt_id, segment_id = _branch_ids(branch_plan, submission_key)

    decision = MeasurementDecisionV1(
        decisionId=_new_decision_id(),
        workflowRunId=original.workflowRunId,
        routeId=route_id,
        sourceSegmentId=original.sourceSegmentId,
        sourceAttemptId=original.sourceAttemptId or source.attemptId,
        sourceCompileId=source_compile_id,
        sourceCompileHash=original.sourceCompileHash,
        sourceRunId=original.sourceRunId,
        startAfterStepId=start_after_step_id,
        sourceCheckpointHash=original.sourceCheckpointHash,
        sourcePrefixDigest=original.sourcePrefixDigest,
        seed=original.seed,
        measurementId=original.measurementId,
        measurementStatus=original.measurementStatus,
        measurementBindingStatus=original.measurementBindingStatus,
        measurementEquivalenceStatus=original.measurementEquivalenceStatus,
        recipeId=original.recipeId,
        recipeVersion=original.recipeVersion,
        metricId=original.metricId,
        method=original.method,
        measuredValue=original.measuredValue,
        unit=original.unit,
        operator=original.operator,
        threshold=original.threshold,
        result=original.result,
        chosenBranch=original.chosenBranch,
        candidateParameters=original.candidateParameters,
        resolvedParameters=original.resolvedParameters,
        targetCompileId=original.targetCompileId,
        targetCompileHash=original.targetCompileHash,
        branchAttemptId=attempt_id,
        branchSegmentId=segment_id,
        branchThroughStepId=through,
        replayOf=original.decisionId,
        createdAt=_now(),
    )
    stored = store.save_measurement_decision(seal_measurement_decision(decision))
    return {
        "decision": stored,
        "plan": branch_plan,
        "workflowRunId": stored.workflowRunId,
        "measurement": measurement,
        "resolvedParameters": stored.resolvedParameters,
    }


def list_workflow_runs(
    store: WorkflowStore, route_id: str
) -> list[WorkflowRunSummaryV1]:
    """Group the route's decisions into the workflow runs they belong to."""

    grouped: dict[str, list[MeasurementDecisionV1]] = {}
    for decision in store.list_measurement_decisions(route_id):
        grouped.setdefault(decision.workflowRunId, []).append(decision)
    return [
        WorkflowRunSummaryV1(
            workflowRunId=workflow_run_id,
            routeId=route_id,
            decisionCount=len(decisions),
            replayCount=sum(1 for item in decisions if item.replayOf is not None),
            decisionIds=[item.decisionId for item in decisions],
            branchSegmentIds=[item.branchSegmentId for item in decisions],
            firstDecisionAt=decisions[0].createdAt,
            lastDecisionAt=decisions[-1].createdAt,
        )
        for workflow_run_id, decisions in grouped.items()
    ]


def get_workflow_run(
    store: WorkflowStore, route_id: str, workflow_run_id: str
) -> WorkflowRunDetailV1:
    """Return one workflow run as decisions plus the lineage they reference."""

    decisions = [
        decision
        for decision in store.list_measurement_decisions(route_id)
        if decision.workflowRunId == workflow_run_id
    ]
    if not decisions:
        raise WorkflowNotFound(
            f"workflow run {workflow_run_id!r} has no decision on route {route_id!r}"
        )
    plan = store.get_plan(route_id)
    segment_ids = {
        segment_id
        for decision in decisions
        for segment_id in (decision.sourceSegmentId, decision.branchSegmentId)
    }
    segments = [
        segment for segment in plan.segments if segment.segmentId in segment_ids
    ]
    attempt_ids = {decision.branchAttemptId for decision in decisions}
    attempt_ids.update(segment.attemptId for segment in segments)
    attempts = [
        attempt for attempt in plan.attempts if attempt.attemptId in attempt_ids
    ]
    return WorkflowRunDetailV1(
        workflowRunId=workflow_run_id,
        routeId=route_id,
        decisions=decisions,
        attempts=attempts,
        segments=segments,
    )
