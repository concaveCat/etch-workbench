"""Conformance harness for v2 plugin variants.

:func:`check_variant` answers one question: *can this registered variant run
inside the v2 runtime without breaking the contract?* The checks are
deliberately mechanical:

* the registry resolves the process/variant IDs and ``params`` is an instance
  of the process's ``params_model``;
* ``variant.validate`` returns :class:`~process_models.v2.contract.Issue`
  values, and any ``error`` severity fails conformance;
* ``execute_plan`` returns a :class:`~process_models.v2.state.StepResult` whose
  ``delta`` is a :class:`~process_models.v2.state.StateDelta` and whose
  ``outputs`` is an :class:`~process_models.v2.state.OutputSet`;
* a variant that does not declare ``mutates_input=True`` leaves the input
  state's geometry/layers/fields handles unchanged (identity plus a best-effort
  content fingerprint - native objects are opaque, so this cannot be a proof);
* two runs from fresh ``state_factory()`` states agree when the variant
  declares ``determinism="strict"``.

It is **not** a physics test: numeric parity and process-specific acceptance
stay with the plugin. Only the engine requirement declared by the variant is
acquired, through the provider supplied by the caller.

Determinism
-----------
For ``determinism="strict"`` the harness compares the two runs with the
optional ``digest`` callback when one is supplied. Otherwise it compares the
``OutputSet``, the ``StepResult.metadata``, and the *shape* of the state delta
(representation presence plus contract fields such as dimension, grid, layer
ids, and field contracts). Handles are never compared by native geometry
equality because they are opaque and may not implement ``__eq__``.

``digest`` is an optional keyword-only extension to the frozen v2.2 signature;
callers that do not pass it keep the exact documented call shape. It receives
``(result, committed_state)`` and must return a comparable value (a string or
tuple is the safe choice).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

import numpy as np
from pydantic import BaseModel

from .contract import Issue, ProcessContext, ProcessSpec, VariantRegistration
from .engine import EngineProvider
from .registry import ProcessRegistry
from .resolver import ExecutionOutcome, ExecutionRequest, execute_plan, resolve_execution
from .state import (
    CellSetState,
    LayerState,
    LevelSetState,
    MaterialState,
    OutputSet,
    ProcessState,
    StateDelta,
    StepResult,
    VoxelState,
)

if TYPE_CHECKING:
    from .adapters import StateAdapter

__all__ = ["ConformanceReport", "check_variant"]

DigestCallback = Callable[[StepResult, ProcessState], Any]


@dataclass(frozen=True)
class ConformanceReport:
    """Outcome of one :func:`check_variant` call.

    ``issues`` holds only failures; advisory validation findings and variant
    warnings are recorded under ``details["warnings"]``. ``passed`` is therefore
    exactly ``not issues``. ``details`` is a JSON-friendly summary: resolved
    plan identity, validation findings, output counts, state/delta summaries,
    determinism comparison, and warnings. Opaque handles never appear in it.
    """

    process_id: str
    variant_id: str
    passed: bool
    issues: list[str]
    details: dict[str, Any]


def check_variant(
    registry: ProcessRegistry,
    provider: EngineProvider,
    process_id: str,
    variant_id: str,
    params: BaseModel,
    context: ProcessContext,
    state_factory: Callable[[], ProcessState],
    adapter: StateAdapter | None = None,
    *,
    digest: DigestCallback | None = None,
) -> ConformanceReport:
    """Run the v2 conformance checks for one registered variant.

    Two runs are always attempted, each from a fresh ``state_factory()`` state.
    ``digest`` is only used for ``determinism="strict"`` variants; see the
    module docstring for its call shape and the structural fallback.
    """
    issues: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {
        "process_id": process_id,
        "variant_id": variant_id,
        "plan_identity": None,
        "validation": None,
        "mutates_input": None,
        "output_counts": None,
        "outputs": None,
        "state": None,
        "delta": None,
        "determinism": None,
        "warnings": warnings,
    }

    def report() -> ConformanceReport:
        return ConformanceReport(
            process_id=process_id,
            variant_id=variant_id,
            passed=not issues,
            issues=list(issues),
            details=details,
        )

    try:
        resolved = registry.get_variant(process_id, variant_id)
    except KeyError as error:
        issues.append(f"registry does not resolve {process_id!r}/{variant_id!r}: {error}")
        return report()

    process: ProcessSpec = resolved.process
    variant: VariantRegistration = resolved.variant

    if not isinstance(params, process.params_model):
        issues.append(f"params must be an instance of {process.params_model.__name__}, got {type(params).__name__}")
        return report()

    try:
        findings = list(variant.variant.validate(context, params))
    except Exception as error:  # noqa: BLE001 - a crashing validator is a conformance failure
        issues.append(f"variant.validate raised {type(error).__name__}: {error}")
        return report()

    validation: list[dict[str, Any]] = []
    errors = 0
    for finding in findings:
        if not isinstance(finding, Issue):
            issues.append(f"variant.validate returned {type(finding).__name__}, expected Issue")
            continue
        validation.append({"path": finding.path, "message": finding.message, "severity": finding.severity})
        if finding.severity == "error":
            errors += 1
            issues.append(f"validation rejected the request: {_issue_text(finding)}")
        else:
            warnings.append(_issue_text(finding))
    details["validation"] = {
        "errors": errors,
        "warnings": len(validation) - errors,
        "issues": validation,
    }
    if errors:
        return report()

    details["mutates_input"] = bool(variant.capabilities.mutates_input)
    outcomes: list[ExecutionOutcome] = []

    for index in range(2):
        label = f"run {index + 1}"
        try:
            state = state_factory()
        except Exception as error:  # noqa: BLE001 - a broken factory is a conformance failure
            issues.append(f"{label}: state_factory raised {type(error).__name__}: {error}")
            break
        if not isinstance(state, ProcessState):
            issues.append(f"{label}: state_factory returned {type(state).__name__}, expected ProcessState")
            break

        before = _handle_snapshot(state)
        request = ExecutionRequest(
            process_id=process_id,
            variant_id=variant_id,
            params=params,
            context=context,
            state=state,
        )

        try:
            plan = resolve_execution(request, registry, provider, adapter=adapter)
        except Exception as error:  # noqa: BLE001 - resolution failures are reported, not raised
            issues.append(f"{label}: resolution failed: {type(error).__name__}: {error}")
            break
        if details["plan_identity"] is None:
            details["plan_identity"] = plan.identity()

        try:
            outcome = execute_plan(plan)
        except Exception as error:  # noqa: BLE001 - execution failures are reported, not raised
            issues.append(f"{label}: execution failed: {type(error).__name__}: {error}")
            break

        result = outcome.result
        if not isinstance(result, StepResult):
            issues.append(f"{label}: variant returned {type(result).__name__}, expected StepResult")
            break
        if not isinstance(result.delta, StateDelta):
            issues.append(f"{label}: StepResult.delta must be a StateDelta, got {type(result.delta).__name__}")
            break
        if not isinstance(result.outputs, OutputSet):
            issues.append(f"{label}: StepResult.outputs must be an OutputSet, got {type(result.outputs).__name__}")
            break
        outcomes.append(outcome)

        if not variant.capabilities.mutates_input:
            after = _handle_snapshot(state)
            changed = sorted(name for name, token in before.items() if after.get(name) != token)
            if changed:
                issues.append(
                    f"{label}: variant does not declare mutates_input=True but changed "
                    f"input state handles: {', '.join(changed)}"
                )

        if index == 0:
            details["outputs"] = _output_summary(result.outputs)
            details["output_counts"] = {
                "metrology": len(result.outputs.metrology),
                "artifacts": len(result.outputs.artifacts),
            }
            details["state"] = _state_summary(outcome.state)
            details["delta"] = _delta_shape(result.delta)
            warnings.extend(result.warnings)

    details["determinism"] = _determinism_details(outcomes, variant, digest, issues)
    return report()


def _determinism_details(
    outcomes: list[ExecutionOutcome],
    variant: VariantRegistration,
    digest: DigestCallback | None,
    issues: list[str],
) -> dict[str, Any]:
    """Compare the two runs and append failures to ``issues``."""
    mode = variant.capabilities.determinism
    details: dict[str, Any] = {"mode": mode, "checked": False, "compared_with": None, "differences": []}

    if len(outcomes) < 2:
        details["skipped"] = "second run did not complete"
        return details
    if mode != "strict":
        return details

    details["checked"] = True
    if digest is not None:
        details["compared_with"] = "digest"
        differences = _compare_with_digest(outcomes[0], outcomes[1], digest)
    else:
        details["compared_with"] = "structure"
        differences = _compare_structurally(outcomes[0], outcomes[1])
    details["differences"] = differences
    for difference in differences:
        issues.append(f"determinism (strict): {difference}")
    return details


def _compare_with_digest(
    first: ExecutionOutcome,
    second: ExecutionOutcome,
    digest: DigestCallback,
) -> list[str]:
    """Compare caller-supplied digests for two runs."""
    try:
        left = digest(first.result, first.state)
        right = digest(second.result, second.state)
    except Exception as error:  # noqa: BLE001 - a crashing digest is a conformance failure
        return [f"digest callback raised {type(error).__name__}: {error}"]
    if _values_equal(left, right):
        return []
    return [f"digest differs between runs: {left!r} vs {right!r}"]


def _compare_structurally(first: ExecutionOutcome, second: ExecutionOutcome) -> list[str]:
    """Compare outputs, metadata, and delta shape (never native handles)."""
    differences: list[str] = []
    if not _values_equal(first.result.outputs, second.result.outputs):
        differences.append("OutputSet differs between runs")
    if not _values_equal(first.result.metadata, second.result.metadata):
        differences.append("StepResult.metadata differs between runs")
    if not _values_equal(_delta_shape(first.result.delta), _delta_shape(second.result.delta)):
        differences.append("state delta shape differs between runs")
    return differences


def _values_equal(left: Any, right: Any) -> bool:
    """Compare values without letting numpy arrays decide the truth value."""
    if isinstance(left, np.ndarray) or isinstance(right, np.ndarray):
        try:
            return bool(np.array_equal(left, right))
        except Exception:  # noqa: BLE001 - incomparable payloads are unequal
            return False
    try:
        outcome = left == right
    except Exception:  # noqa: BLE001 - incomparable payloads are unequal
        return False
    if isinstance(outcome, bool):
        return outcome
    try:
        return bool(outcome)
    except Exception:  # noqa: BLE001 - ambiguous truth is unequal by definition
        return False


def _issue_text(issue: Issue) -> str:
    return f"{issue.path}: {issue.message}" if issue.path else issue.message


def _handle_snapshot(state: ProcessState) -> dict[str, tuple[int, str]]:
    """Record ``(id, fingerprint)`` for the input handles of ``state``.

    A variant normally mutates a native handle in place, so id identity alone
    is not enough; the fingerprint is a best-effort content check (``repr`` for
    opaque objects, a hash for numpy arrays). Native objects that do not expose
    their content in ``repr`` can still evade this check.
    """
    snapshot: dict[str, tuple[int, str]] = {}

    def record(name: str, value: Any) -> None:
        snapshot[name] = (id(value), _fingerprint(value))

    if state.geometry is not None:
        record("geometry", state.geometry)
        record("geometry.handle", state.geometry.handle)
    if state.layers is not None:
        record("layers", state.layers)
    if state.fields is not None:
        record("fields", state.fields)
        record("fields.material_ids", state.fields.material_ids)
        for name, item in state.fields.fields.items():
            record(f"fields.{name}.data", item.data)
    return snapshot


def _fingerprint(value: Any) -> str:
    """Return a cheap, comparable fingerprint for an opaque handle."""
    if isinstance(value, np.ndarray):
        try:
            digest = hashlib.sha256(np.ascontiguousarray(value).tobytes()).hexdigest()
        except Exception:  # noqa: BLE001 - fall back to the shape when bytes fail
            return f"ndarray:{value.dtype}:{tuple(value.shape)}"
        return f"ndarray:{value.dtype}:{tuple(value.shape)}:{digest}"
    try:
        return repr(value)
    except Exception:  # noqa: BLE001 - a broken repr still identifies the object
        return f"<unrepr {type(value).__name__}>"


def _delta_shape(delta: StateDelta) -> dict[str, Any]:
    """Return the shape of a delta: presence plus contract fields, no handles."""
    shape: dict[str, Any] = {}
    if delta.geometry is not None:
        shape["geometry"] = _geometry_shape(delta.geometry)
    if delta.materials is not None:
        shape["materials"] = _materials_shape(delta.materials)
    if delta.layers is not None:
        shape["layers"] = _layers_shape(delta.layers)
    if delta.fields is not None:
        shape["fields"] = _cellset_shape(delta.fields)
    if delta.voxels is not None:
        shape["voxels"] = _voxels_shape(delta.voxels)
    return shape


def _geometry_shape(geometry: LevelSetState) -> dict[str, Any]:
    return {
        "dimension": geometry.dimension,
        "grid_delta": geometry.grid_delta,
    }


def _materials_shape(materials: MaterialState) -> dict[str, Any]:
    return {
        "order": list(materials.order),
        "names": {str(material_id): name for material_id, name in materials.names.items()},
    }


def _layers_shape(layers: LayerState) -> list[dict[str, Any]]:
    return [
        {
            "layer_id": layer.layer_id,
            "material_id": layer.material_id,
            "name": layer.name,
            "source_step": layer.source_step,
            "thickness": layer.thickness,
        }
        for layer in layers.layers
    ]


def _cellset_shape(cells: CellSetState) -> dict[str, Any]:
    grid = cells.grid
    return {
        "grid": {
            "dimension": grid.dimension,
            "origin": list(grid.origin),
            "spacing": grid.spacing,
            "shape": list(grid.shape),
        },
        "material_ids": _array_descriptor(cells.material_ids),
        "fields": {
            name: {
                "unit": item.unit,
                "association": item.association,
                "dtype": item.dtype,
                "shape": list(item.shape),
                "components": item.components,
            }
            for name, item in cells.fields.items()
        },
        "provenance": sorted(str(key) for key in cells.provenance),
    }


def _voxels_shape(voxels: VoxelState) -> dict[str, Any]:
    order = [int(material_id) for material_id in getattr(voxels.grid, "material_order", ())]
    return {
        "source": voxels.source,
        "material": _array_descriptor(getattr(voxels.grid, "material", None)),
        "material_order": order,
    }


def _array_descriptor(value: Any) -> dict[str, Any] | None:
    """Describe an array or opaque handle by dtype/shape, never by payload."""
    if value is None:
        return None
    dtype = getattr(value, "dtype", None)
    shape = getattr(value, "shape", None)
    return {
        "dtype": str(dtype) if dtype is not None else type(value).__name__,
        "shape": _shape_list(shape),
    }


def _shape_list(shape: Any) -> list[int] | None:
    if shape is None:
        return None
    try:
        return [int(entry) for entry in shape]
    except (TypeError, ValueError):
        return None


def _state_summary(state: ProcessState) -> dict[str, Any]:
    """Summarize the committed state without exposing opaque handles."""
    geometry = state.geometry
    layers = state.layers
    cells = state.fields
    voxels = state.voxels
    return {
        "geometry": None if geometry is None else _geometry_shape(geometry),
        "materials": _materials_shape(state.materials),
        "layers": None if layers is None else [layer.layer_id for layer in layers.layers],
        "fields": None if cells is None else _cellset_shape(cells),
        "voxels": None if voxels is None else _voxels_shape(voxels),
    }


def _output_summary(outputs: OutputSet) -> dict[str, Any]:
    return {
        "schema_version": outputs.schema_version,
        "metrology": [
            {
                "metric": record.metric,
                "value": record.value,
                "unit": record.unit,
                "region": record.region,
                "method": record.method,
                "uncertainty": record.uncertainty,
            }
            for record in outputs.metrology
        ],
        "artifacts": [
            {
                "kind": artifact.kind,
                "name": artifact.name,
                "path": artifact.path,
                "schema_version": artifact.schema_version,
            }
            for artifact in outputs.artifacts
        ],
    }
