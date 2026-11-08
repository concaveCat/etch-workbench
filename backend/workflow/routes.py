"""Route/Attempt/Segment plans and prefix compatibility for P0-2."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from ..models import StrictModel

Id = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
StepId = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
Hash64 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Status = Literal["planned", "running", "succeeded", "failed", "cancelled", "interrupted"]


class CheckpointRefV1(StrictModel):
    checkpointId: Id
    runId: Id
    stepId: StepId
    stateHash: Hash64


class PrefixCompatibilityV1(StrictModel):
    """Identity of the executable prefix that a branch is allowed to reuse.

    ``prefixDigest`` covers the ordered resolved prefix steps and geometry
    binding. It deliberately does not cover steps after the prefix.
    """

    prefixDigest: Hash64
    geometryBindingHash: Hash64
    solverDigest: Hash64
    seedPolicy: Literal["shared-run-seed"]
    runSeed: Annotated[int, Field(ge=0, le=4294967295)]
    stepIds: Annotated[list[StepId], Field(min_length=1)]
    resolvedStepDigest: Hash64


class RouteV1(StrictModel):
    routeId: Id
    flowId: Id
    name: Annotated[str, Field(min_length=1, max_length=80)]
    headAttemptId: Id
    legacyUnverified: bool = False


class AttemptV1(StrictModel):
    attemptId: Id
    routeId: Id
    parentAttemptId: Id | None = None
    compileId: Annotated[str, Field(pattern=r"^c_[a-f0-9]{24}$")]
    submissionKey: Id
    baseCheckpointRef: CheckpointRefV1 | None = None
    startAfterStepId: StepId | None = None
    throughStepId: StepId
    changedStepIds: list[StepId] = Field(default_factory=list)
    prefixCompatibility: PrefixCompatibilityV1 | None = None
    seedPolicy: Literal["shared-run-seed"] = "shared-run-seed"
    runSeed: Annotated[int, Field(ge=0, le=4294967295)]
    flowDigest: Hash64
    status: Status = "planned"
    legacyCompatibility: dict[str, Any] | None = None


class SegmentV1(StrictModel):
    segmentId: Id
    attemptId: Id
    parentSegmentId: Id | None = None
    sourceCompileId: Annotated[str, Field(pattern=r"^c_[a-f0-9]{24}$")]
    segmentProjectHash: Hash64
    startAfterStepId: StepId | None = None
    throughStepId: StepId
    startCheckpointRef: CheckpointRefV1 | None = None
    endCheckpointRef: CheckpointRefV1 | None = None
    runId: Id
    provenanceInputHash: Hash64
    flowDigest: Hash64
    executionHash: Hash64
    resultDigest: Hash64 | None = None
    status: Status = "planned"

    @model_validator(mode="after")
    def completed_segment_boundaries(self) -> "SegmentV1":
        if self.endCheckpointRef is not None and self.endCheckpointRef.stepId != self.throughStepId:
            raise ValueError("end checkpoint must belong to throughStepId")
        if self.startAfterStepId is None:
            if self.startCheckpointRef is not None:
                raise ValueError("an initial segment must not have a start checkpoint")
        else:
            if self.startCheckpointRef is not None and self.startCheckpointRef.stepId != self.startAfterStepId:
                raise ValueError("start checkpoint must belong to startAfterStepId")
            if self.status in {"running", "succeeded"} and self.startCheckpointRef is None:
                raise ValueError(f"{self.status} segment requires a start checkpoint")
        if self.status == "succeeded" and self.endCheckpointRef is None:
            raise ValueError("succeeded segment requires an end checkpoint")
        return self


class SegmentReuseV1(StrictModel):
    reuseId: Id
    attemptId: Id
    reusedFromSegmentId: Id
    fromStepId: StepId
    toStepId: StepId
    compatibility: PrefixCompatibilityV1
    legacyCompatibility: dict[str, Any] | None = None


class SegmentRefV1(StrictModel):
    segmentId: Id
    reused: bool
    fromStepId: StepId
    toStepId: StepId


class RoutePlanV1(StrictModel):
    route: RouteV1
    stepOrder: Annotated[list[StepId], Field(min_length=1)]
    attempts: Annotated[list[AttemptV1], Field(min_length=1)]
    segments: list[SegmentV1]
    reuse: list[SegmentReuseV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_plan(self) -> "RoutePlanV1":
        _validate_plan(self)
        return self


def _index(step_order: list[str]) -> dict[str, int]:
    if len(step_order) != len(set(step_order)):
        raise ValueError("stepOrder must not contain duplicate step ids")
    return {step_id: index for index, step_id in enumerate(step_order)}


def _range_for(start_after: str | None, through: str, index: dict[str, int]) -> tuple[int, int]:
    if through not in index:
        raise ValueError(f"throughStepId {through!r} is not in stepOrder")
    end = index[through]
    if start_after is None:
        start = 0
    else:
        if start_after not in index:
            raise ValueError(f"startAfterStepId {start_after!r} is not in stepOrder")
        start = index[start_after] + 1
    if start > end:
        raise ValueError(f"segment range after {start_after!r} through {through!r} is empty")
    return start, end


def _attempts_by_id(plan: RoutePlanV1) -> dict[str, AttemptV1]:
    result: dict[str, AttemptV1] = {}
    for attempt in plan.attempts:
        if attempt.attemptId in result:
            raise ValueError(f"duplicate attemptId {attempt.attemptId!r}")
        result[attempt.attemptId] = attempt
    return result


def _segments_by_id(plan: RoutePlanV1) -> dict[str, SegmentV1]:
    result: dict[str, SegmentV1] = {}
    for segment in plan.segments:
        if segment.segmentId in result:
            raise ValueError(f"duplicate segmentId {segment.segmentId!r}")
        result[segment.segmentId] = segment
    return result


def _check_parent_cycles(plan: RoutePlanV1) -> None:
    attempts = _attempts_by_id(plan)
    for attempt in plan.attempts:
        seen: set[str] = set()
        current: AttemptV1 | None = attempt
        while current is not None:
            if current.attemptId in seen:
                raise ValueError(f"attempt parent cycle includes {current.attemptId!r}")
            seen.add(current.attemptId)
            if current.parentAttemptId is None:
                break
            parent = attempts.get(current.parentAttemptId)
            if parent is None:
                raise ValueError(f"attempt {current.attemptId!r} references missing parent {current.parentAttemptId!r}")
            current = parent


def _check_segment_parent_cycles(plan: RoutePlanV1, segments: dict[str, SegmentV1]) -> None:
    for segment in plan.segments:
        seen: set[str] = set()
        current: SegmentV1 | None = segment
        while current is not None:
            if current.segmentId in seen:
                raise ValueError(f"segment parent cycle includes {current.segmentId!r}")
            seen.add(current.segmentId)
            if current.parentSegmentId is None:
                break
            parent = segments.get(current.parentSegmentId)
            if parent is None:
                raise ValueError(f"segment {current.segmentId!r} references missing parent {current.parentSegmentId!r}")
            if parent.attemptId != current.attemptId:
                raise ValueError(f"segment {current.segmentId!r} parent belongs to another attempt")
            if parent.throughStepId != current.startAfterStepId:
                raise ValueError(f"segment {current.segmentId!r} does not follow its parent step range")
            if parent.endCheckpointRef is not None and current.startCheckpointRef is not None:
                if parent.endCheckpointRef != current.startCheckpointRef:
                    raise ValueError(f"segment {current.segmentId!r} checkpoint does not follow its parent")
            current = parent


def _attempt_own_segments(plan: RoutePlanV1) -> dict[str, list[SegmentV1]]:
    result: dict[str, list[SegmentV1]] = {attempt.attemptId: [] for attempt in plan.attempts}
    for segment in plan.segments:
        if segment.attemptId not in result:
            raise ValueError(f"segment {segment.segmentId!r} references an unknown attempt")
        result[segment.attemptId].append(segment)
    return result


def _check_reuse(
    plan: RoutePlanV1,
    attempt: AttemptV1,
    *,
    index: dict[str, int],
    segments: dict[str, SegmentV1],
) -> str | None:
    """Validate the reused prefix and return the last reused segment id."""

    start, _end = _range_for(attempt.startAfterStepId, attempt.throughStepId, index)
    entries = sorted(
        (item for item in plan.reuse if item.attemptId == attempt.attemptId),
        key=lambda item: index[item.fromStepId],
    )
    if not entries:
        if attempt.prefixCompatibility is not None and not plan.route.legacyUnverified:
            raise ValueError(f"attempt {attempt.attemptId!r} declares prefix compatibility without reuse")
        return None
    if attempt.startAfterStepId is None:
        raise ValueError(f"attempt {attempt.attemptId!r} reuses a prefix without a base checkpoint")
    if attempt.prefixCompatibility is None and not plan.route.legacyUnverified:
        raise ValueError(f"attempt {attempt.attemptId!r} reuses a prefix without compatibility")
    expected = 0
    for entry in entries:
        if entry.fromStepId not in index or entry.toStepId not in index:
            raise ValueError(f"reuse {entry.reuseId!r} references a step outside stepOrder")
        from_index = index[entry.fromStepId]
        to_index = index[entry.toStepId]
        if from_index != expected:
            raise ValueError(f"reuse prefix for {attempt.attemptId!r} is not contiguous from step 0")
        if to_index < from_index:
            raise ValueError(f"reuse {entry.reuseId!r} has an inverted range")
        source = segments.get(entry.reusedFromSegmentId)
        if source is None:
            raise ValueError(f"reuse {entry.reuseId!r} references a missing segment")
        if source.attemptId == attempt.attemptId:
            raise ValueError(f"reuse {entry.reuseId!r} references the same attempt")
        if source.status != "succeeded":
            raise ValueError(f"reuse {entry.reuseId!r} references a non-succeeded segment")
        source_start, source_end = _range_for(source.startAfterStepId, source.throughStepId, index)
        if source_start != from_index or source_end != to_index:
            raise ValueError(f"reuse {entry.reuseId!r} range does not match the source segment")
        if not plan.route.legacyUnverified:
            if entry.compatibility.stepIds != plan.stepOrder[from_index:to_index + 1]:
                raise ValueError(
                    f"reuse {entry.reuseId!r} compatibility stepIds do not match its range"
                )
            prefix = attempt.prefixCompatibility
            if prefix is None:
                raise ValueError(f"attempt {attempt.attemptId!r} reuses a prefix without compatibility")
            if (
                entry.compatibility.geometryBindingHash != prefix.geometryBindingHash
                or entry.compatibility.solverDigest != prefix.solverDigest
                or entry.compatibility.seedPolicy != prefix.seedPolicy
                or entry.compatibility.runSeed != prefix.runSeed
            ):
                raise ValueError(
                    f"reuse {entry.reuseId!r} compatibility envelope differs from the attempt prefix"
                )
        expected = to_index + 1
    if expected != start:
        raise ValueError(f"reuse prefix for {attempt.attemptId!r} does not end at startAfterStepId")
    if not plan.route.legacyUnverified and attempt.prefixCompatibility is not None:
        if attempt.prefixCompatibility.stepIds != plan.stepOrder[:start]:
            raise ValueError(f"attempt {attempt.attemptId!r} prefixCompatibility stepIds do not match the reused prefix")
    return entries[-1].reusedFromSegmentId


def _check_attempt_segments(
    plan: RoutePlanV1,
    attempt: AttemptV1,
    *,
    index: dict[str, int],
    own_segments: list[SegmentV1],
    reused_from_segment: str | None,
    segments: dict[str, SegmentV1],
) -> None:
    start, end = _range_for(attempt.startAfterStepId, attempt.throughStepId, index)
    ordered = sorted(own_segments, key=lambda item: _range_for(item.startAfterStepId, item.throughStepId, index)[0])
    expected = start
    previous: SegmentV1 | None = None
    for segment in ordered:
        segment_start, segment_end = _range_for(segment.startAfterStepId, segment.throughStepId, index)
        if segment_start != expected:
            raise ValueError(f"segments for attempt {attempt.attemptId!r} are not contiguous at step {plan.stepOrder[expected]!r}")
        if segment.sourceCompileId != attempt.compileId:
            raise ValueError(f"segment {segment.segmentId!r} does not use the attempt compileId")
        if segment.flowDigest != attempt.flowDigest:
            raise ValueError(f"segment {segment.segmentId!r} flowDigest does not match the attempt")
        if expected == start and attempt.startAfterStepId is not None:
            if segment.startCheckpointRef != attempt.baseCheckpointRef:
                raise ValueError(f"segment {segment.segmentId!r} does not start at the attempt base checkpoint")
            if reused_from_segment is not None:
                source = segments[reused_from_segment]
                if segment.startCheckpointRef != source.endCheckpointRef:
                    raise ValueError(f"segment {segment.segmentId!r} does not start at the reused prefix end")
        if previous is not None and previous.endCheckpointRef is not None and segment.startCheckpointRef is not None:
            if previous.endCheckpointRef != segment.startCheckpointRef:
                raise ValueError(f"segment {segment.segmentId!r} checkpoint does not follow the previous segment")
        expected = segment_end + 1
        previous = segment
    if expected != end + 1:
        raise ValueError(f"segments for attempt {attempt.attemptId!r} do not cover through {attempt.throughStepId!r}")


def _validate_plan(plan: RoutePlanV1) -> None:
    index = _index(plan.stepOrder)
    attempts = _attempts_by_id(plan)
    segments = _segments_by_id(plan)
    if plan.route.headAttemptId not in attempts:
        raise ValueError("route headAttemptId does not reference an attempt")
    _check_parent_cycles(plan)
    _check_segment_parent_cycles(plan, segments)
    own_by_attempt = _attempt_own_segments(plan)

    for attempt in plan.attempts:
        if attempt.routeId != plan.route.routeId:
            raise ValueError(f"attempt {attempt.attemptId!r} belongs to another route")
        if attempt.runSeed < 0:
            raise ValueError("runSeed must be non-negative")
        start, end = _range_for(attempt.startAfterStepId, attempt.throughStepId, index)
        for changed in attempt.changedStepIds:
            if changed not in index:
                raise ValueError(f"changed step {changed!r} is not in stepOrder")
            if index[changed] < start or index[changed] > end:
                raise ValueError(f"changed step {changed!r} is outside the attempt range")
        if attempt.parentAttemptId is not None:
            parent = attempts[attempt.parentAttemptId]
            parent_start, parent_end = _range_for(parent.startAfterStepId, parent.throughStepId, index)
            if start < parent_start or end > parent_end:
                raise ValueError(f"attempt {attempt.attemptId!r} range is not inside its parent")
        reused_from_segment = _check_reuse(plan, attempt, index=index, segments=segments)
        _check_attempt_segments(
            plan,
            attempt,
            index=index,
            own_segments=own_by_attempt[attempt.attemptId],
            reused_from_segment=reused_from_segment,
            segments=segments,
        )


def materialize_attempt(plan: RoutePlanV1, attempt_id: str) -> list[SegmentRefV1]:
    """Return the ordered segment chain, including reused prefix segments."""

    attempt = next((item for item in plan.attempts if item.attemptId == attempt_id), None)
    if attempt is None:
        raise ValueError(f"attempt {attempt_id!r} does not exist")
    index = _index(plan.stepOrder)
    entries = sorted(
        (item for item in plan.reuse if item.attemptId == attempt_id),
        key=lambda item: index[item.fromStepId],
    )
    refs = [
        SegmentRefV1(
            segmentId=entry.reusedFromSegmentId,
            reused=True,
            fromStepId=entry.fromStepId,
            toStepId=entry.toStepId,
        )
        for entry in entries
    ]
    own = sorted(
        (item for item in plan.segments if item.attemptId == attempt_id),
        key=lambda item: _range_for(item.startAfterStepId, item.throughStepId, index)[0],
    )
    for segment in own:
        start, _end = _range_for(segment.startAfterStepId, segment.throughStepId, index)
        refs.append(
            SegmentRefV1(
                segmentId=segment.segmentId,
                reused=False,
                fromStepId=plan.stepOrder[start],
                toStepId=segment.throughStepId,
            )
        )
    return refs
