"""P1/P2 experiment documents, serial queue execution and metric aggregation."""

from __future__ import annotations

import csv
import io
import itertools
import json
import math
import statistics
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import TypeAdapter

from ..models import SolverV7, StepV7
from ..supervisor import ActiveRunError
from .adoption import adopt_project, first_route
from .compiler import compile_flow
from .execution import SegmentBusy, execute_segment
from .models import FlowRevisionV1, GeometryBindingV1
from .store import WorkflowConflict, WorkflowNotFound, WorkflowStore

DEFAULT_ESTIMATED_SECONDS = 1.0
_T95_TWO_SIDED = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
    27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}
_ALLOWED_FACTORS = {
    "duration", "rateMagnitude", "stickingProbability", "sourceExponent",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _apply_parameters(project: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    clone = json.loads(json.dumps(project))
    for step in clone.get("steps", []):
        overrides = parameters.get(step.get("id"))
        if isinstance(overrides, dict):
            step.update(overrides)
    return clone


def authored_project(compiled: Any) -> dict[str, Any]:
    """Restore authored step ids while keeping the compiled geometry/domain."""

    payload = compiled.project.model_dump(mode="json", exclude_none=True)
    steps = []
    for index, entry in enumerate(compiled.sourceMap):
        step = compiled.project.steps[index].model_dump(mode="json", exclude_none=True)
        step["id"] = entry.authoredStepId
        steps.append(step)
    payload["steps"] = steps
    return payload


def resolve_experiment_base(store: WorkflowStore, project: dict[str, Any], flow_id: str, compile_id: str | None):
    if not compile_id:
        return project, flow_id, None, None
    compiled = store.get_compile(compile_id)
    binding = store.get_binding(compiled.compileRecord.geometryBindingId)
    return authored_project(compiled), compiled.compileRecord.flowId, compiled.compileRecord.compileId, binding


def _replicates(document: dict[str, Any]):
    for variant in document.get("variants") or []:
        for replicate in variant.get("replicates") or []:
            yield variant, replicate


def _elapsed(replicate: dict[str, Any]) -> float | None:
    started, finished = replicate.get("startedAt"), replicate.get("finishedAt")
    if not started or not finished:
        return None
    try:
        return max(0.0, (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds())
    except ValueError:
        return None


def _queue_summary(document: dict[str, Any]) -> dict[str, Any]:
    rows = list(_replicates(document))
    statuses = [str(replicate.get("status") or "planned") for _variant, replicate in rows]
    pending = sum(1 for status in statuses if status in {"planned", "queued"})
    running = sum(1 for status in statuses if status == "running")
    succeeded = sum(1 for status in statuses if status == "succeeded")
    failed = sum(1 for status in statuses if status == "failed")
    durations = [value for _variant, replicate in rows if (value := _elapsed(replicate)) is not None]
    average = statistics.fmean(durations) if durations else DEFAULT_ESTIMATED_SECONDS
    attempts = sum(int(replicate.get("attemptCount") or 0) for _variant, replicate in rows)
    return {
        "total": len(rows),
        "pending": pending,
        "running": running,
        "succeeded": succeeded,
        "failed": failed,
        "executionAttempts": attempts,
        "retries": sum(max(0, int(replicate.get("attemptCount") or 0) - 1) for _variant, replicate in rows),
        "estimatedSecondsRemaining": round((pending + running) * average, 3),
        "estimatedCostUsd": round((pending + running) * float(document.get("costPerRunUsd") or 0.0), 6),
        "averageRunSeconds": round(average, 6),
    }


def _aggregate(document: dict[str, Any]) -> dict[str, Any]:
    unit = document.get("unit") or "um"
    rows = []
    for variant in document.get("variants") or []:
        values: list[float] = []
        for replicate in variant.get("replicates") or []:
            if replicate.get("statisticalSample") and replicate.get("status") == "succeeded":
                value = replicate.get("value")
                if isinstance(value, (int, float)) and math.isfinite(float(value)):
                    values.append(float(value))
        count = len(values)
        mean = statistics.fmean(values) if count else None
        standard_deviation = statistics.stdev(values) if count >= 2 else (0.0 if count == 1 else None)
        ci95 = None
        if count >= 2 and standard_deviation is not None:
            critical = _T95_TWO_SIDED.get(count - 1, 1.96)
            ci95 = critical * standard_deviation / math.sqrt(count)
        rows.append({
            "variantId": variant.get("variantId"),
            "ordinal": variant.get("ordinal"),
            "parameters": variant.get("parameters") or {},
            "n": count,
            "mean": mean,
            "std": standard_deviation,
            "ci95": ci95,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "unit": unit,
            "statisticalStatus": "evaluated" if count >= 2 else "not_evaluated",
        })
    return {
        "metricId": document.get("metricId"),
        "unit": unit,
        "ciMethod": "student-t 95% two-sided",
        "assumptions": ["independent replicates", "approximately normal observations"],
        "sampleSource": "successful replicates with statisticalSample=true",
        "rows": rows,
        "generatedAt": _now(),
    }


def _save(store: WorkflowStore, document: dict[str, Any], *, prefix: str = "") -> None:
    document["updatedAt"] = _now()
    document["queue"] = _queue_summary(document)
    document["aggregation"] = _aggregate(document)
    store.save_experiment(document["experimentId"], document)


def _execute_replicate(
    store: WorkflowStore,
    supervisor: Any,
    document: dict[str, Any],
    variant: dict[str, Any],
    replicate: dict[str, Any],
) -> None:
    try:
        project = _apply_parameters(document["project"], variant.get("parameters") or {})
        project.setdefault("solver", {})["random"] = {"mode": "fixed", "seed": int(replicate["seed"])}
        if document.get("binding"):
            binding = GeometryBindingV1.model_validate(document["binding"])
            steps = [TypeAdapter(StepV7).validate_python(step) for step in project["steps"]]
            solver = SolverV7.model_validate(project["solver"])
            flow = FlowRevisionV1(
                schemaVersion=1, flowId=document["flowId"], revision=1,
                name=document["name"], steps=steps, solver=solver,
            )
            compiled = compile_flow(flow, binding)
            store.register_binding(binding)
        else:
            _flow, binding, compiled = adopt_project(project, flow_id=document["flowId"])
            store.register_binding(binding)
        store.register_compile(compiled)
        plan = first_route(compiled, name=f"{document['name']} · v{variant['ordinal']}")
        store.create_plan(plan)
        segment = plan.segments[0]
        replicate.update({
            "status": "queued", "routeId": plan.route.routeId,
            "segmentId": segment.segmentId,
        })
        replicate.pop("reason", None)
        _save(store, document)
    except Exception as exc:
        replicate.update({"status": "failed", "reason": f"preparation failed: {exc}", "finishedAt": _now()})
        _save(store, document)
        return

    # Waiting for the single supervisor slot is not an execution attempt.
    while True:
        try:
            _segment, run_id = execute_segment(store, supervisor, segment.segmentId)
            break
        except (ActiveRunError, SegmentBusy):
            replicate["status"] = "queued"
            replicate["reason"] = "waiting for an available run slot"
            _save(store, document)
            time.sleep(1.0)
        except Exception as exc:
            replicate.update({"status": "failed", "reason": f"start failed: {exc}", "finishedAt": _now()})
            _save(store, document)
            return

    replicate["attemptCount"] = int(replicate.get("attemptCount") or 0) + 1
    replicate["status"] = "running"
    replicate["startedAt"] = _now()
    replicate["runId"] = run_id
    replicate.pop("reason", None)
    _save(store, document)
    try:
        timeout_seconds = float((document.get("project", {}).get("solver") or {}).get("timeoutSeconds") or 60)
        deadline = time.monotonic() + max(30.0, timeout_seconds + 30.0)
        current = store.get_segment(segment.segmentId)
        while current.status in {"planned", "running", "cancelling"}:
            if time.monotonic() > deadline:
                try:
                    supervisor.cancel(run_id)
                except Exception:
                    pass
                time.sleep(1.0)
                current = store.get_segment(segment.segmentId)
                if current.status in {"planned", "running", "cancelling"}:
                    raise RuntimeError("segment exceeded its run timeout budget")
                break
            time.sleep(0.2)
            current = store.get_segment(segment.segmentId)
        if current.status != "succeeded":
            raise RuntimeError(f"segment finished with {current.status}")
        recipe = store.get_recipe(document["recipeId"], document["recipeVersion"])
        metric = store.get_metric(recipe.metricId)
        measurement = store.measure_run(recipe, metric, run_id, segment.throughStepId)
        bound = store.bind_measurement(measurement.measurementId, segment.segmentId, run_id, segment.throughStepId)
        replicate.update({
            "status": "succeeded",
            "statisticalSample": True,
            "finishedAt": _now(),
            "measurementId": bound.measurementId,
            "value": bound.value,
            "unit": bound.unit,
            "measurementStatus": bound.status,
            "bindingStatus": bound.bindingStatus,
        })
        store.register_artifact_ref(run_id, "experiment", document["experimentId"], "experiment")
        store.register_artifact_ref(run_id, "experiment", f"{document['experimentId']}:{replicate['replicateId']}", "replicate")
    except Exception as exc:
        replicate["reason"] = str(exc)
        replicate["finishedAt"] = _now()
        max_attempts = max(1, int(document.get("maxAttempts") or 1))
        replicate["status"] = "queued" if int(replicate["attemptCount"]) < max_attempts else "failed"
    _save(store, document)


_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()
_EXECUTION_LOCK = threading.Lock()


def _worker(store: WorkflowStore, supervisor: Any, experiment_id: str) -> None:
    try:
        document = store.get_experiment(experiment_id)
        document["status"] = "running"
        _save(store, document)
        while True:
            document = store.get_experiment(experiment_id)
            job = next(
                (
                    (variant, replicate)
                    for variant, replicate in _replicates(document)
                    if str(replicate.get("status") or "planned") in {"planned", "queued"}
                ),
                None,
            )
            if job is None:
                break
            with _EXECUTION_LOCK:
                _execute_replicate(store, supervisor, document, job[0], job[1])
        document = store.get_experiment(experiment_id)
        document["status"] = "completed" if all(
            replicate.get("status") == "succeeded"
            for _variant, replicate in _replicates(document)
        ) else "failed"
        _save(store, document)
    except Exception as exc:
        try:
            document = store.get_experiment(experiment_id)
            document["status"] = "failed"
            document["reason"] = str(exc)
            _save(store, document)
        except Exception:
            pass
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(experiment_id)


def start_experiment(store: WorkflowStore, supervisor: Any, experiment_id: str) -> dict[str, Any]:
    document = store.get_experiment(experiment_id)
    with _ACTIVE_LOCK:
        if experiment_id in _ACTIVE:
            return document
        _ACTIVE.add(experiment_id)
    thread = threading.Thread(target=_worker, args=(store, supervisor, experiment_id), daemon=True)
    thread.start()
    return document


def resume_experiments(store: WorkflowStore, supervisor: Any) -> None:
    """Resume experiments that were queued/running when the server stopped."""

    for summary in store.list_experiments():
        try:
            document = store.get_experiment(summary["experimentId"])
        except WorkflowNotFound:
            continue
        running = [replicate for _variant, replicate in _replicates(document) if replicate.get("status") == "running"]
        if document.get("status") not in {"queued", "running"} and not running:
            continue
        changed = False
        for _variant, replicate in _replicates(document):
            if replicate.get("status") == "running":
                replicate["status"] = "queued"
                replicate["reason"] = "interrupted by server restart"
                changed = True
        document["status"] = "queued"
        if changed:
            _save(store, document)
        else:
            store.save_experiment(document["experimentId"], document)
        start_experiment(store, supervisor, document["experimentId"])


def _new_replicate(seed: int) -> dict[str, Any]:
    return {
        "replicateId": f"rep_{uuid.uuid4().hex[:12]}",
        "seed": int(seed),
        "status": "planned",
        "statisticalSample": False,
        "attemptCount": 0,
    }


def _apply_base(document: dict[str, Any], base_compile_id: str | None, binding: Any) -> None:
    document["baseCompileId"] = base_compile_id
    document["bindingId"] = binding.bindingId if binding is not None else None
    document["binding"] = binding.model_dump(mode="json", exclude_none=True) if binding is not None else None
    document["retention"] = {"mode": "keep-all", "updatedAt": _now()}


def create_experiment_document(
    *,
    name: str,
    project: dict[str, Any],
    flow_id: str,
    recipe_id: str,
    recipe_version: int,
    variants: list[dict[str, Any]],
    metric_id: str = "metric",
    unit: str = "um",
    max_attempts: int = 1,
    cost_per_run_usd: float = 0.0,
    base_compile_id: str | None = None,
    binding: Any = None,
) -> dict[str, Any]:
    experiment_id = f"exp_{uuid.uuid4().hex[:16]}"
    rows = []
    for ordinal, variant in enumerate(variants, start=1):
        replicates = [
            _new_replicate(int(variant.get("seed", 20261029)) + index)
            for index in range(int(variant.get("replicates", 1)))
        ]
        rows.append({
            "variantId": f"var_{uuid.uuid4().hex[:12]}",
            "ordinal": ordinal,
            "parameters": variant.get("parameters") or {},
            "replicates": replicates,
        })
    document = {
        "schemaVersion": 2,
        "kind": "manual",
        "experimentId": experiment_id,
        "name": name,
        "flowId": flow_id,
        "project": project,
        "recipeId": recipe_id,
        "recipeVersion": recipe_version,
        "metricId": metric_id,
        "unit": unit,
        "maxAttempts": max(1, int(max_attempts)),
        "costPerRunUsd": float(cost_per_run_usd),
        "variants": rows,
        "status": "planned",
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    _apply_base(document, base_compile_id, binding)
    document["queue"] = _queue_summary(document)
    document["aggregation"] = _aggregate(document)
    return document


def create_doe_document(
    *,
    name: str,
    project: dict[str, Any],
    flow_id: str,
    recipe_id: str,
    recipe_version: int,
    factors: list[dict[str, Any]],
    replicates: int,
    seed: int = 20261029,
    metric_id: str = "metric",
    unit: str = "um",
    max_attempts: int = 2,
    cost_per_run_usd: float = 0.0,
    base_compile_id: str | None = None,
    binding: Any = None,
) -> dict[str, Any]:
    step_by_id = {str(step.get("id")): step for step in project.get("steps") or []}
    validated: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for factor in factors:
        step_id = str(factor["stepId"])
        parameter = str(factor["parameter"])
        values = factor["values"]
        if (step_id, parameter) in seen:
            raise ValueError(f"DOE factors must be unique per (stepId, parameter): {step_id}/{parameter}")
        seen.add((step_id, parameter))
        if step_id not in step_by_id:
            raise ValueError(f"DOE factor step {step_id!r} does not exist in the project")
        if parameter not in _ALLOWED_FACTORS:
            raise ValueError(f"DOE factor {parameter!r} is not a supported numeric step parameter")
        if parameter not in step_by_id[step_id]:
            raise ValueError(f"DOE factor step {step_id!r} has no {parameter!r}")
        normalized = [float(value) for value in values]
        if not normalized or any(not math.isfinite(value) for value in normalized):
            raise ValueError(f"DOE factor {parameter!r} values must be finite")
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"DOE factor {parameter!r} values must be unique")
        validated.append({"stepId": step_id, "parameter": parameter, "values": normalized})
    combos = list(itertools.product(*[factor["values"] for factor in validated]))
    if len(combos) > 256:
        raise ValueError("DOE full factorial design exceeds the 256 condition limit")
    variants = []
    for ordinal, combo in enumerate(combos, start=1):
        parameters: dict[str, dict[str, float]] = {}
        for factor, value in zip(validated, combo):
            parameters.setdefault(factor["stepId"], {})[factor["parameter"]] = value
        variants.append({
            "variantId": f"var_{uuid.uuid4().hex[:12]}",
            "ordinal": ordinal,
            "parameters": parameters,
            "replicates": [_new_replicate(seed + (ordinal - 1) * 100 + index) for index in range(replicates)],
        })
    document = {
        "schemaVersion": 2,
        "kind": "doe",
        "experimentId": f"exp_{uuid.uuid4().hex[:16]}",
        "name": name,
        "flowId": flow_id,
        "project": project,
        "recipeId": recipe_id,
        "recipeVersion": recipe_version,
        "metricId": metric_id,
        "unit": unit,
        "maxAttempts": max(1, int(max_attempts)),
        "costPerRunUsd": float(cost_per_run_usd),
        "design": {
            "method": "full-factorial",
            "factors": validated,
            "replicates": int(replicates),
            "seed": int(seed),
            "conditionCount": len(combos),
            "logicalRunCount": len(combos) * int(replicates),
        },
        "variants": variants,
        "status": "planned",
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    _apply_base(document, base_compile_id, binding)
    document["queue"] = _queue_summary(document)
    document["aggregation"] = _aggregate(document)
    return document


def experiment_table(
    document: dict[str, Any],
    *,
    min_value: float | None = None,
    max_value: float | None = None,
    sort: str = "ordinal",
    order: str = "asc",
) -> dict[str, Any]:
    rows = list((document.get("aggregation") or {}).get("rows") or [])
    if min_value is not None:
        rows = [row for row in rows if row.get("mean") is not None and float(row["mean"]) >= min_value]
    if max_value is not None:
        rows = [row for row in rows if row.get("mean") is not None and float(row["mean"]) <= max_value]
    key = sort if sort in {"ordinal", "mean", "std", "min", "max", "n"} else "ordinal"
    filtered = [row for row in rows if row.get(key) is not None]
    missing = [row for row in rows if row.get(key) is None]
    filtered.sort(key=lambda row: row[key], reverse=order == "desc")
    rows = filtered + missing
    for row in rows:
        variant = next(
            (item for item in document.get("variants") or [] if item.get("variantId") == row.get("variantId")),
            None,
        )
        row["replicates"] = [
            {
                "replicateId": replicate.get("replicateId"),
                "seed": replicate.get("seed"),
                "status": replicate.get("status"),
                "value": replicate.get("value"),
                "unit": replicate.get("unit"),
                "runId": replicate.get("runId"),
                "statisticalSample": bool(replicate.get("statisticalSample")),
                "attemptCount": int(replicate.get("attemptCount") or 0),
            }
            for replicate in (variant or {}).get("replicates") or []
        ]
    return {
        "experimentId": document.get("experimentId"),
        "name": document.get("name"),
        "status": document.get("status"),
        "metricId": document.get("metricId"),
        "unit": document.get("unit"),
        "queue": document.get("queue") or _queue_summary(document),
        "rows": rows,
    }


def experiment_csv(document: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "experiment_id", "variant_id", "ordinal", "parameters", "replicate_id", "seed",
        "status", "value", "unit", "run_id", "statistical_sample", "attempt_count",
        "n", "mean", "std", "ci95", "min", "max",
    ])
    table = experiment_table(document)
    by_variant = {row["variantId"]: row for row in table["rows"]}
    for variant in document.get("variants") or []:
        summary = by_variant.get(variant.get("variantId"), {})
        for replicate in variant.get("replicates") or []:
            writer.writerow([
                document.get("experimentId"), variant.get("variantId"), variant.get("ordinal"),
                json.dumps(variant.get("parameters") or {}, sort_keys=True, separators=(",", ":")),
                replicate.get("replicateId"), replicate.get("seed"), replicate.get("status"),
                replicate.get("value"), replicate.get("unit"), replicate.get("runId"),
                bool(replicate.get("statisticalSample")), int(replicate.get("attemptCount") or 0),
                summary.get("n"), summary.get("mean"), summary.get("std"), summary.get("ci95"),
                summary.get("min"), summary.get("max"),
            ])
    return buffer.getvalue()


def _owned_ids(document: dict[str, Any]) -> tuple[set[str], set[str]]:
    measurements: set[str] = set()
    segments: set[str] = set()
    for _variant, replicate in _replicates(document):
        if replicate.get("measurementId"):
            measurements.add(str(replicate["measurementId"]))
        if replicate.get("segmentId"):
            segments.add(str(replicate["segmentId"]))
    return measurements, segments


def retention_preview(store: WorkflowStore, document: dict[str, Any]) -> dict[str, Any]:
    experiment_id = document["experimentId"]
    own_measurements, own_segments = _owned_ids(document)
    releasable: list[str] = []
    protected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _variant, replicate in _replicates(document):
        run_id = replicate.get("runId")
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        dependencies = store.artifact_dependencies(str(run_id))
        reasons: list[str] = []
        if dependencies["pinned"]:
            reasons.append("artifact is pinned")
        for ref in dependencies["refs"]:
            if ref["ownerType"] == "experiment" and ref["ownerId"] in {
                experiment_id, f"{experiment_id}:{replicate['replicateId']}",
            }:
                continue
            reasons.append(f"referenced by {ref['ownerType']} {ref['ownerId']} ({ref['requiredFor']})")
        for measurement_id in dependencies["measurements"]:
            if measurement_id not in own_measurements:
                reasons.append(f"measurement {measurement_id} is not owned by this experiment")
        for segment_id in dependencies["segmentOwners"]:
            if segment_id not in own_segments:
                reasons.append(f"segment {segment_id} is not owned by this experiment")
        for segment_id in dependencies["checkpointOwners"]:
            if segment_id not in own_segments:
                reasons.append(f"checkpoint reused by segment {segment_id}")
        if reasons:
            protected.append({"runId": run_id, "reasons": reasons})
        else:
            releasable.append(str(run_id))
    return {
        "experimentId": experiment_id,
        "mode": "release-runs",
        "releasable": releasable,
        "protected": protected,
    }


def apply_experiment_retention(store: WorkflowStore, document: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode not in {"keep-all", "release-runs"}:
        raise ValueError("retention mode must be keep-all or release-runs")
    experiment_id = document["experimentId"]
    preview = retention_preview(store, document)
    if mode == "release-runs":
        for _variant, replicate in _replicates(document):
            run_id = replicate.get("runId")
            if not run_id or run_id not in preview["releasable"]:
                continue
            store.abandon_capability(
                str(run_id), "experiment", experiment_id, "experiment",
                "experiment retention policy released run artifacts",
            )
            store.abandon_capability(
                str(run_id), "experiment", f"{experiment_id}:{replicate['replicateId']}", "replicate",
                "experiment retention policy released replicate artifacts",
            )
            if replicate.get("measurementId"):
                store.abandon_capability(
                    str(run_id), "measurement", str(replicate["measurementId"]), "remeasure",
                    "experiment retention keeps the metric value, not the run",
                )
            if replicate.get("segmentId"):
                for capability in ("report", "continue"):
                    store.abandon_capability(
                        str(run_id), "segment", str(replicate["segmentId"]), capability,
                        "experiment retention keeps the metric value, not the run",
                    )
            replicate["retentionReleased"] = True
    else:
        for _variant, replicate in _replicates(document):
            run_id = replicate.get("runId")
            if not run_id:
                continue
            store.register_artifact_ref(str(run_id), "experiment", experiment_id, "experiment")
            store.register_artifact_ref(str(run_id), "experiment", f"{experiment_id}:{replicate['replicateId']}", "replicate")
            replicate["retentionReleased"] = False
    document["retention"] = {"mode": mode, "updatedAt": _now()}
    _save(store, document)
    return {**preview, "mode": mode, "applied": True}
