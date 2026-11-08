"""Shared P0-2/P0-3 fixtures built from real compiled envelopes."""

from __future__ import annotations

from backend.models import (
    AdvectionSettingsV7,
    Domain,
    Domain3D,
    FixedRandom,
    RayTracingSettingsV7,
    SolverV7,
    Units,
)
from backend.workflow import (
    AttemptV1,
    CheckpointRefV1,
    CompiledWorkflowV1,
    FlowRevisionV1,
    GeometryBindingV1,
    RoutePlanV1,
    SegmentReuseV1,
    SegmentV1,
    compile_flow,
)
from backend.workflow.hashing import full_project_hash
from backend.workflow.verification import prefix_compatibility


UNITS = Units(length="um", time="s", rate="um/s", angle="deg")
STEP_IDS = ["step_001", "step_002", "step_003", "step_004", "step_005"]


def _binding() -> GeometryBindingV1:
    return GeometryBindingV1(
        schemaVersion=1,
        bindingId="bind_fixture_01",
        name="fixture trench",
        dimension=3,
        units=UNITS,
        domain=Domain3D(gridDelta=0.2, xExtent=4.0, yExtent=4.0, boundaryXY="reflective"),
        geometry={
            "kind": "trench",
            "width": 1.0,
            "depth": 1.2,
            "taperAngle": 0.0,
            "maskHeight": 0.0,
            "maskTaperAngle": 0.0,
            "substrateMaterial": "Si",
            "maskMaterial": "Mask",
        },
    )


def _solver(
    *,
    spatial_scheme: str = "ENGQUIST_OSHER_1ST_ORDER",
    rays_per_point: int = 32,
) -> SolverV7:
    return SolverV7(
        timeStepRatio=0.25,
        raysPerPoint=rays_per_point,
        cpuThreads=1,
        random=FixedRandom(mode="fixed", seed=314159),
        enginePreference="cpu",
        timeoutSeconds=60,
        advection=AdvectionSettingsV7(spatialScheme=spatial_scheme),
        rayTracing=RayTracingSettingsV7(),
    )


def _step(step_id: str, duration: float) -> dict:
    return {
        "id": step_id,
        "name": step_id,
        "enabled": True,
        "operation": "etch",
        "model": "isotropic",
        "rateMagnitude": 0.2,
        "duration": duration,
        "rateMode": "uniform",
    }


def _flow(revision: int, step4_duration: float, solver: SolverV7 | None = None) -> FlowRevisionV1:
    durations = [0.2, 0.2, 0.2, step4_duration, 0.2]
    return FlowRevisionV1(
        schemaVersion=1,
        flowId="flow_fixture_01",
        revision=revision,
        name="fixture flow",
        steps=[_step(step_id, duration) for step_id, duration in zip(STEP_IDS, durations)],
        solver=solver or _solver(),
    )


def _binding_2d() -> GeometryBindingV1:
    return GeometryBindingV1(
        schemaVersion=1,
        bindingId="bind_fixture_2d",
        name="fixture 2D plane",
        dimension=2,
        units=UNITS,
        domain=Domain(gridDelta=0.2, xExtent=4.0, xBoundary="reflective"),
        geometry={"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
    )


def compiled_2d() -> CompiledWorkflowV1:
    return compile_flow(_flow(1, 0.2), _binding_2d())


def compiled_2d_zero() -> CompiledWorkflowV1:
    return _compiled_zero(1)


def compiled_2d_zero_two() -> CompiledWorkflowV1:
    return _compiled_zero(2)


def compiled_2d_zero_five() -> CompiledWorkflowV1:
    return _compiled_zero(5)


def compiled_2d_particle(step5_duration: float) -> CompiledWorkflowV1:
    steps = [
        {"id": "step_p01", "name": "prep", "enabled": True, "operation": "etch", "model": "isotropic", "rateMagnitude": 0.005, "duration": 0.02, "rateMode": "uniform"},
        {"id": "step_p02", "name": "particle", "enabled": True, "operation": "etch", "model": "singleParticle", "rateMagnitude": 0.01, "duration": 0.01, "stickingProbability": 0.1, "sourceExponent": 1.0, "rateMode": "uniform"},
        {"id": "step_p03", "name": "clean", "enabled": True, "operation": "deposit", "model": "isotropic", "rateMagnitude": 0.002, "duration": 0.02, "depositMaterial": "SiO2", "rateMode": "uniform"},
        {"id": "step_p04", "name": "cap", "enabled": True, "operation": "deposit", "model": "isotropic", "rateMagnitude": 0.002, "duration": 0.02, "depositMaterial": "SiO2", "rateMode": "uniform"},
        {"id": "step_p05", "name": "tail particle", "enabled": True, "operation": "etch", "model": "singleParticle", "rateMagnitude": 0.01, "duration": step5_duration, "stickingProbability": 0.1, "sourceExponent": 1.0, "rateMode": "uniform"},
    ]
    return compile_flow(
        FlowRevisionV1(
            schemaVersion=1, flowId="flow_particle_2d", revision=1,
            name="particle branch flow", steps=steps,
            solver=SolverV7(timeStepRatio=0.25, raysPerPoint=16, cpuThreads=1,
                            random=FixedRandom(mode="fixed", seed=12345),
                            enginePreference="cpu", timeoutSeconds=60),
        ),
        _binding_2d(),
    )


def _compiled_zero(count: int) -> CompiledWorkflowV1:
    steps = [{
        "id": f"step_zero_{index:02d}",
        "name": f"no-op {index + 1}",
        "enabled": True,
        "operation": "etch",
        "model": "isotropic",
        "rateMagnitude": 0.0,
        "duration": 0.01,
        "rateMode": "uniform",
    } for index in range(count)]
    return compile_flow(
        FlowRevisionV1(
            schemaVersion=1,
            flowId="flow_zero_2d_01" if count == 1 else "flow_zero_2d_02",
            revision=1,
            name="zero-rate 2D flow",
            steps=steps,
            solver=_solver(),
        ),
        _binding_2d(),
    )


def binding_plan_2d(compiled: CompiledWorkflowV1, *, suffix: str, run_id: str, state_hash: str) -> RoutePlanV1:
    step_ids = [step.id for step in compiled.project.steps]
    end = _checkpoint("cp_end", step_ids[-1], "e")
    end.stateHash = state_hash
    attempt = AttemptV1(
        attemptId=f"A{suffix}",
        routeId=f"R{suffix}",
        compileId=compiled.compileRecord.compileId,
        submissionKey=f"sub{suffix}",
        startAfterStepId=None,
        throughStepId=step_ids[-1],
        seedPolicy="shared-run-seed",
        runSeed=compiled.project.solver.random.seed,
        flowDigest=compiled.compileRecord.fullProjectHash,
        status="succeeded",
    )
    segment = SegmentV1(
        segmentId=f"S{suffix}",
        attemptId=attempt.attemptId,
        sourceCompileId=compiled.compileRecord.compileId,
        segmentProjectHash=full_project_hash(compiled.project),
        startAfterStepId=None,
        throughStepId=step_ids[-1],
        startCheckpointRef=None,
        endCheckpointRef=end,
        runId=run_id,
        provenanceInputHash="a" * 64,
        flowDigest=compiled.compileRecord.fullProjectHash,
        executionHash="e" * 64,
        resultDigest="d" * 64,
        status="succeeded",
    )
    return RoutePlanV1(
        route={"routeId": f"R{suffix}", "flowId": "flow_fixture_01", "name": "binding route", "headAttemptId": attempt.attemptId},
        stepOrder=step_ids,
        attempts=[attempt],
        segments=[segment],
        reuse=[],
    )


def compiled_base() -> CompiledWorkflowV1:
    return compile_flow(_flow(1, 0.2), _binding())


def compiled_branch() -> CompiledWorkflowV1:
    return compile_flow(_flow(2, 0.7), _binding())


def compiled_with_solver(*, spatial_scheme: str = "WENO_3RD_ORDER", rays_per_point: int = 64) -> CompiledWorkflowV1:
    return compile_flow(
        _flow(3, 0.2, _solver(spatial_scheme=spatial_scheme, rays_per_point=rays_per_point)),
        _binding(),
    )


def _checkpoint(name: str, step_id: str, char: str) -> CheckpointRefV1:
    return CheckpointRefV1(
        checkpointId=name,
        runId="run_fixture_01",
        stepId=step_id,
        stateHash=char * 64,
    )


def plan_from_compiled(
    base: CompiledWorkflowV1,
    branch: CompiledWorkflowV1,
    *,
    suffix: str,
) -> RoutePlanV1:
    step_ids = [step.id for step in base.project.steps]
    cp3 = _checkpoint("cp3", step_ids[2], "3")
    cp5 = _checkpoint("cp5", step_ids[4], "5")
    prefix = prefix_compatibility(branch, step_ids[:3], 314159)
    source_prefix = prefix_compatibility(base, step_ids[:3], 314159)

    a1 = AttemptV1(
        attemptId=f"A{suffix}1",
        routeId=f"R{suffix}",
        compileId=base.compileRecord.compileId,
        submissionKey=f"sub{suffix}a1",
        startAfterStepId=None,
        throughStepId=step_ids[4],
        seedPolicy="shared-run-seed",
        runSeed=314159,
        flowDigest=base.compileRecord.fullProjectHash,
        status="succeeded",
    )
    a2 = AttemptV1(
        attemptId=f"A{suffix}2",
        routeId=f"R{suffix}",
        parentAttemptId=a1.attemptId,
        compileId=branch.compileRecord.compileId,
        submissionKey=f"sub{suffix}a2",
        baseCheckpointRef=cp3,
        startAfterStepId=step_ids[2],
        throughStepId=step_ids[4],
        changedStepIds=[step_ids[3]],
        prefixCompatibility=prefix,
        seedPolicy="shared-run-seed",
        runSeed=314159,
        flowDigest=branch.compileRecord.fullProjectHash,
        status="planned",
    )
    s1 = SegmentV1(
        segmentId=f"S{suffix}1",
        attemptId=a1.attemptId,
        sourceCompileId=base.compileRecord.compileId,
        segmentProjectHash=base.compileRecord.fullProjectHash,
        startAfterStepId=None,
        throughStepId=step_ids[2],
        startCheckpointRef=None,
        endCheckpointRef=cp3,
        runId=f"run{suffix}1",
        provenanceInputHash="a" * 64,
        flowDigest=base.compileRecord.fullProjectHash,
        executionHash="e" * 64,
        resultDigest="d" * 64,
        status="succeeded",
    )
    s2 = SegmentV1(
        segmentId=f"S{suffix}2",
        attemptId=a1.attemptId,
        parentSegmentId=s1.segmentId,
        sourceCompileId=base.compileRecord.compileId,
        segmentProjectHash=base.compileRecord.fullProjectHash,
        startAfterStepId=step_ids[2],
        throughStepId=step_ids[4],
        startCheckpointRef=cp3,
        endCheckpointRef=cp5,
        runId=f"run{suffix}2",
        provenanceInputHash="b" * 64,
        flowDigest=base.compileRecord.fullProjectHash,
        executionHash="e" * 64,
        resultDigest="d" * 64,
        status="succeeded",
    )
    s3 = SegmentV1(
        segmentId=f"S{suffix}3",
        attemptId=a2.attemptId,
        sourceCompileId=branch.compileRecord.compileId,
        segmentProjectHash=branch.compileRecord.fullProjectHash,
        startAfterStepId=step_ids[2],
        throughStepId=step_ids[4],
        startCheckpointRef=cp3,
        endCheckpointRef=cp5,
        runId=f"run{suffix}3",
        provenanceInputHash="c" * 64,
        flowDigest=branch.compileRecord.fullProjectHash,
        executionHash="e" * 64,
        resultDigest="d" * 64,
        status="planned",
    )
    reuse = SegmentReuseV1(
        reuseId=f"RU{suffix}",
        attemptId=a2.attemptId,
        reusedFromSegmentId=s1.segmentId,
        fromStepId=step_ids[0],
        toStepId=step_ids[2],
        compatibility=source_prefix,
    )
    return RoutePlanV1(
        route={"routeId": f"R{suffix}", "flowId": "flow_fixture_01", "name": "fixture route", "headAttemptId": a2.attemptId},
        stepOrder=step_ids,
        attempts=[a1, a2],
        segments=[s1, s2, s3],
        reuse=[reuse],
    )


def base_plan_from_compiled(base: CompiledWorkflowV1, *, suffix: str) -> RoutePlanV1:
    plan = plan_from_compiled(base, compiled_branch(), suffix=suffix)
    payload = plan.model_dump(mode="json", exclude_none=True)
    payload["route"]["headAttemptId"] = payload["attempts"][0]["attemptId"]
    payload["attempts"] = payload["attempts"][:1]
    payload["segments"] = payload["segments"][:2]
    payload["reuse"] = []
    return RoutePlanV1.model_validate(payload)
