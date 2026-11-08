"""JSON-serializable contract description for v2 step outputs.

The runtime keeps ``StepResult``/``OutputSet`` as Python values; future API and
UI layers need a stable, language-neutral description of what those values
contain. :func:`output_contract_schema` returns that description.

It is a *contract description*, not a full JSON Schema validator: each type
lists its fields, value types, nullability, and where array payloads live on
disk. The whole document must survive ``json.dumps(..., allow_nan=False)``.
"""

from __future__ import annotations

from typing import Any

from .state import ARTIFACT_KINDS

__all__ = ["output_contract_schema"]

SCHEMA_VERSION = 1


def _value(type_name: str, description: str, *, nullable: bool = False, **extra: Any) -> dict[str, Any]:
    """Build one field description."""
    spec: dict[str, Any] = {"type": type_name}
    if nullable:
        spec["nullable"] = True
    spec["description"] = description
    spec.update(extra)
    return spec


def _record(description: str, fields: dict[str, Any], *, required: tuple[str, ...] = ()) -> dict[str, Any]:
    """Build one type description from its fields."""
    return {"description": description, "fields": fields, "required": list(required)}


def _output_set() -> dict[str, Any]:
    return _record(
        "Typed report data for one step; never part of the flow state.",
        {
            "schemaVersion": _value("integer", "OutputSet schema version.", minimum=1),
            "metrology": _value(
                "array",
                "Measured or simulated metrics.",
                items={"type": "MetrologyRecord"},
            ),
            "artifacts": _value(
                "array",
                "Named files or payloads produced by the step.",
                items={"type": "Artifact"},
            ),
        },
        required=("schemaVersion",),
    )


def _state_delta() -> dict[str, Any]:
    null_note = "null means unchanged for this step."
    return _record(
        "State representations the next step consumes.",
        {
            "geometry": _value(
                "LevelSetState",
                f"Opaque engine level-set handle; {null_note}",
                nullable=True,
                persisted=False,
                note="Persisted through a per-step geometry reference, not inline.",
            ),
            "materials": _value(
                "MaterialState",
                f"Native material IDs in level-set order; {null_note}",
                nullable=True,
            ),
            "layers": _value("LayerState", f"Ordered layer stack; {null_note}", nullable=True),
            "fields": _value("CellSetState", f"Regular cell grid with named fields; {null_note}", nullable=True),
            "voxels": _value(
                "VoxelState",
                f"Dense height-field grid; {null_note}",
                nullable=True,
                persisted=False,
                note="Per-step scratch; never persisted.",
            ),
        },
        required=(),
    )


def _material_state() -> dict[str, Any]:
    return _record(
        "Native material IDs in level-set order plus optional names.",
        {
            "order": _value(
                "array",
                "One native material ID per level-set index; IDs may repeat or be empty.",
                items={"type": "integer", "minimum": 0},
            ),
            "names": _value(
                "object",
                "Display name per material ID (unique names; keys cover order).",
                additionalProperties={"type": "string"},
            ),
        },
        required=("order",),
    )


def _level_set_state() -> dict[str, Any]:
    return _record(
        "An engine-specific level-set handle plus the grid it carries.",
        {
            "handle": _value(
                "LevelSetHandle",
                "Opaque engine handle (for native variants the ViennaPS Domain).",
                persisted=False,
                note="Never serialized; only usable inside the owning engine lease.",
            ),
            "dimension": _value("integer", "Spatial dimension.", enum=[2, 3]),
            "gridDelta": _value("number", "Grid spacing in project units.", exclusiveMinimum=0),
        },
        required=("handle", "dimension", "gridDelta"),
    )


def _voxel_state() -> dict[str, Any]:
    return _record(
        "A dense height-field grid.",
        {
            "grid": _value(
                "VoxelGrid",
                "Dense material/height grid (process_models.voxel.VoxelGrid).",
                persisted=False,
                note="Per-step scratch; serialized through a dedicated artifact when available.",
            ),
            "source": _value("string", "Producer tag, for example 'height-field-v1'.", default="height-field-v1"),
        },
        required=("grid",),
    )


def _field() -> dict[str, Any]:
    return _record(
        "One named scalar/vector/tensor field over a cell grid.",
        {
            "name": _value("string", "Field name; unique inside its CellSetState."),
            "unit": _value("string", "Physical unit, for example '1/cm3' or 'MPa'."),
            "association": _value("string", "Where values live.", enum=["cell", "point"]),
            "dtype": _value("string", "Numpy dtype name, for example 'float64'."),
            "shape": _value(
                "array",
                "Grid shape without the component axis.",
                items={"type": "integer", "minimum": 1},
                minItems=2,
                maxItems=3,
            ),
            "components": _value(
                "integer",
                "1 for scalar, 3 for vector, 6/9 for tensors.",
                minimum=1,
                default=1,
            ),
            "data": _value(
                "array",
                "numpy array with shape or (components, *shape).",
                persisted=False,
                note="Arrays live in the step's fields.npz as field_000, field_001, ... in field order.",
            ),
        },
        required=("name", "unit", "association", "dtype", "shape"),
    )


def _cell_set_state() -> dict[str, Any]:
    return _record(
        "A regular grid with per-cell material IDs and named cell fields.",
        {
            "grid": _value(
                "object",
                "Regular grid metadata.",
                fields={
                    "dimension": {"type": "integer", "enum": [2, 3]},
                    "origin": {"type": "array", "items": {"type": "number"}},
                    "spacing": {"type": "number", "exclusiveMinimum": 0},
                    "shape": {"type": "array", "items": {"type": "integer", "minimum": 1}},
                    "arrayOrder": {"type": "string", "enum": ["C", "F"], "default": "C"},
                },
            ),
            "handle": _value(
                "CellSetHandle",
                "Opaque engine cell-set handle (for native variants a ViennaPS DenseCellSet).",
                nullable=True,
                persisted=False,
                note="Authoritative while a field sequence runs; never serialized.",
            ),
            "domainHandle": _value(
                "CellSetDomainHandle",
                "Opaque engine domain that owns the native cell set; ViennaPS Process needs it.",
                nullable=True,
                persisted=False,
                note="Recreated with the native cell set on rehydration; never serialized.",
            ),
            "materialIds": _value(
                "array",
                "Per-cell integer material IDs.",
                nullable=True,
                items={"type": "integer", "minimum": 0},
                persisted=False,
                note="Stored in the step's fields.npz under 'material_ids'.",
            ),
            "fields": _value(
                "object",
                "Named fields keyed by field name.",
                additionalProperties={"type": "Field"},
            ),
            "provenance": _value("object", "Informational producer metadata.", additionalProperties=True),
        },
        required=("grid", "materialIds", "fields"),
    )


def _layer() -> dict[str, Any]:
    return _record(
        "One layer in a layer stack.",
        {
            "layerId": _value("string", "Stable identifier, unique within its LayerState."),
            "materialId": _value("integer", "Native material ID (>= 0).", minimum=0),
            "name": _value("string", "Human-readable layer name."),
            "sourceStep": _value("string", "Step that created the layer, if known.", nullable=True),
            "thickness": _value("number", "Layer thickness, if known.", nullable=True, exclusiveMinimum=0),
            "metadata": _value("object", "Additional JSON metadata.", additionalProperties=True),
        },
        required=("layerId", "materialId", "name"),
    )


def _layer_state() -> dict[str, Any]:
    return _record(
        "An ordered tuple of layers.",
        {
            "layers": _value(
                "array",
                "Layer entries; the producer documents top-to-bottom or bottom-to-top order.",
                items={"type": "Layer"},
            )
        },
        required=("layers",),
    )


def output_contract_schema() -> dict[str, Any]:
    """Return the v2 output contract description as plain JSON data.

    A fresh dict is built on every call, so callers may mutate or annotate it
    without affecting later calls.
    """
    return {
        "schemaVersion": SCHEMA_VERSION,
        "contract": "process-model-step-result",
        "note": "Contract description for API/UI consumers, not a JSON Schema validator.",
        "types": {
            "StepResult": _record(
                "One step's committed state change plus its report data.",
                {
                    "delta": _value("StateDelta", "State representations the next step consumes."),
                    "outputs": _value("OutputSet", "Metrology records and artifacts."),
                    "metadata": _value("object", "Small per-step bookkeeping values.", additionalProperties=True),
                    "warnings": _value("array", "Human-readable warnings.", items={"type": "string"}),
                },
                required=("delta",),
            ),
            "StateDelta": _state_delta(),
            "OutputSet": _output_set(),
            "MetrologyRecord": _record(
                "One measured or simulated metric.",
                {
                    "metric": _value("string", "Metric name."),
                    "value": _value("number", "Finite measured value."),
                    "unit": _value("string", "Physical unit."),
                    "region": _value("string", "Region the value applies to.", nullable=True),
                    "method": _value("string", "Measurement/simulation method.", default="plugin"),
                    "uncertainty": _value("number", "Non-negative uncertainty.", nullable=True, minimum=0),
                },
                required=("metric", "value", "unit"),
            ),
            "Artifact": _record(
                "A named output produced by a step.",
                {
                    "kind": _value("string", "Artifact kind.", enum=sorted(ARTIFACT_KINDS)),
                    "name": _value("string", "Artifact name."),
                    "path": _value("string", "Relative path, if the artifact is stored on disk.", nullable=True),
                    "schemaVersion": _value("integer", "Payload schema version.", minimum=1, default=1),
                    "metadata": _value("object", "Additional JSON metadata.", additionalProperties=True),
                },
                required=("kind", "name"),
            ),
            "CellSetState": _cell_set_state(),
            "MaterialState": _material_state(),
            "LevelSetState": _level_set_state(),
            "VoxelState": _voxel_state(),
            "Field": _field(),
            "Layer": _layer(),
            "LayerState": _layer_state(),
        },
    }
