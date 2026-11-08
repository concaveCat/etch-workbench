"""On-disk persistence for v2 flow state, outputs, and plan identity.

Layout (all paths below ``<root>``)::

    flow.json                  # caller-supplied run record
    steps/<step_id>/
      state.json               # schema v1, see below
      fields.npz               # only when a CellSetState is present
      outputs.json             # OutputSet (metrology + artifacts), schema v1
      plan.json                # ExecutionPlan.identity_json() (camelCase)
      geometry.vpsd            # optional native geometry, written by the executor

``state.json`` schema v1::

    {
      "schemaVersion": 1,
      "stepId": "...",
      "materials": {"order": [10, 30], "names": {"10": "Si", "30": "SiO2"}},
      "geometry": {"ref": "geometry.vpsd" | null, "dimension": 2,
                   "gridDelta": 0.2} | null,
      "layers": {"layers": [{"layerId": "...", "materialId": 30, "name": "...",
                             "sourceStep": "...", "thickness": 0.1,
                             "metadata": {}}]} | null,
      "fields": {"grid": {"dimension": 2, "origin": [-1.0, -1.0],
                          "spacing": 0.1, "shape": [20, 20],
                          "arrayOrder": "F"},
                 "fields": {"P_total": {"unit": "1/cm3", "association": "cell",
                                        "dtype": "float64", "shape": [20, 20],
                                        "components": 1}},
                 "provenance": {},
                 "materialIds": {"dtype": "int32", "shape": [20, 20]}} | null
    }

The ``materialIds`` descriptor is only written when ``CellSetState.material_ids``
is set; the array itself lives in ``fields.npz`` under ``material_ids``. Named
field arrays are stored as ``field_000``, ``field_001``, ... in JSON field order.
``arrayOrder`` is omitted for the default ``"C"``; a native cell-set snapshot
uses ``"F"`` so the flat x-fastest native stream maps to ``[i, j, k]``. Opaque
``handle``/``domain_handle`` values are never serialized.

``outputs.json`` schema v1::

    {
      "schemaVersion": 1,
      "metrology": [{"metric": "...", "value": 1.0, "unit": "nm",
                     "region": null, "method": "plugin",
                     "uncertainty": null}],
      "artifacts": [{"kind": "json", "name": "...", "path": null,
                     "schemaVersion": 1, "metadata": {}}]
    }

The two schema versions are independent: ``state.json`` is governed by
:data:`STATE_SCHEMA_VERSION` and ``outputs.json`` by
:data:`OUTPUTS_SCHEMA_VERSION`. Bumping one never invalidates files written by
the other, and both are validated on load against their own constant.

Rules:

* Level-set handles are opaque and are never serialized. A caller that wants
  the geometry back passes ``geometry_ref`` on save and a ``geometry_loader``
  on load; a ref without a loader raises :class:`StateStoreError`.
* ``voxels`` are per-step scratch and are not persisted.
* Field data (and ``material_ids``) must be numpy arrays; opaque handles raise
  :class:`StateStoreError`.
* Every JSON write is atomic (``tmp`` file plus :func:`os.replace`) and rejects
  NaN/Inf or non-serializable values with :class:`StateStoreError`.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from .state import (
    Artifact,
    CellSetState,
    Field,
    GridSpec,
    Layer,
    LayerState,
    LevelSetState,
    MaterialState,
    MetrologyRecord,
    OutputSet,
    ProcessState,
)

if TYPE_CHECKING:
    from .resolver import ExecutionPlan

__all__ = ["StateStore", "StateStoreError"]

STATE_SCHEMA_VERSION = 1
OUTPUTS_SCHEMA_VERSION = 1
STATE_FILE = "state.json"
FIELDS_FILE = "fields.npz"
OUTPUTS_FILE = "outputs.json"
PLAN_FILE = "plan.json"
FLOW_FILE = "flow.json"

_GEOMETRY_LOADER = Callable[[str], Any]


class StateStoreError(RuntimeError):
    """Raised when a state or output cannot be written or read back."""


def _validate_step_id(step_id: Any) -> str:
    """Return ``step_id`` as a plain directory name or raise."""
    if not isinstance(step_id, str) or not step_id.strip():
        raise StateStoreError(f"step id must be a non-empty string, got {step_id!r}")
    if step_id in {".", ".."} or "/" in step_id or "\\" in step_id:
        raise StateStoreError(f"step id must be a single path component, got {step_id!r}")
    return step_id


def _dumps(payload: Any, *, what: str) -> str:
    """Serialize ``payload`` as strict JSON (no NaN/Inf) or raise."""
    try:
        return json.dumps(payload, allow_nan=False, indent=2) + "\n"
    except (TypeError, ValueError) as error:
        raise StateStoreError(f"{what} is not JSON-serializable: {error}") from error


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temporary file plus ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise StateStoreError(f"could not write {path}: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_arrays(path: Path, arrays: dict[str, np.ndarray]) -> None:
    """Write numpy arrays to a ``.npz`` file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            np.savez(stream, **arrays)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise StateStoreError(f"could not write {path}: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path, *, what: str) -> Any:
    """Read one JSON document or raise :class:`StateStoreError`."""
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError as error:
        raise StateStoreError(f"{what} is missing at {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise StateStoreError(f"{what} at {path} is unreadable: {error}") from error


def _read_arrays(path: Path, *, what: str) -> dict[str, np.ndarray]:
    """Read a ``.npz`` archive with pickles disabled or raise."""
    try:
        with np.load(path, allow_pickle=False) as archive:
            return {name: archive[name] for name in archive.files}
    except FileNotFoundError as error:
        raise StateStoreError(f"{what} is missing at {path}") from error
    except (OSError, ValueError) as error:
        raise StateStoreError(f"{what} at {path} is unreadable: {error}") from error


def _materials_payload(materials: MaterialState) -> dict[str, Any]:
    """Serialize a :class:`MaterialState` (names keyed by string id)."""
    return {
        "order": [int(material_id) for material_id in materials.order],
        "names": {str(material_id): name for material_id, name in sorted(materials.names.items())},
    }


def _materials_from_payload(payload: Any) -> MaterialState:
    """Rebuild a :class:`MaterialState` from ``state.json``."""
    if payload is None:
        return MaterialState(())
    try:
        return MaterialState(
            order=tuple(int(material_id) for material_id in payload.get("order") or ()),
            names={int(material_id): str(name) for material_id, name in (payload.get("names") or {}).items()},
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise StateStoreError(f"invalid materials payload: {error}") from error


def _geometry_payload(geometry: LevelSetState | None, geometry_ref: str | None) -> dict[str, Any] | None:
    """Serialize geometry metadata plus the optional on-disk reference.

    Material order lives only in ``materials``; it is never duplicated here.
    """
    if geometry is None:
        return None
    return {
        "ref": geometry_ref,
        "dimension": int(geometry.dimension),
        "gridDelta": float(geometry.grid_delta),
    }


def _layers_payload(layers: LayerState | None) -> dict[str, Any] | None:
    """Serialize a :class:`LayerState`."""
    if layers is None:
        return None
    return {
        "layers": [
            {
                "layerId": layer.layer_id,
                "materialId": int(layer.material_id),
                "name": layer.name,
                "sourceStep": layer.source_step,
                "thickness": None if layer.thickness is None else float(layer.thickness),
                "metadata": dict(layer.metadata),
            }
            for layer in layers.layers
        ]
    }


def _layers_from_payload(payload: Any) -> LayerState | None:
    """Rebuild a :class:`LayerState` from ``state.json``."""
    if payload is None:
        return None
    try:
        return LayerState(
            tuple(
                Layer(
                    layer_id=item["layerId"],
                    material_id=item["materialId"],
                    name=item["name"],
                    source_step=item.get("sourceStep"),
                    thickness=item.get("thickness"),
                    metadata=item.get("metadata") or {},
                )
                for item in payload.get("layers") or ()
            )
        )
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise StateStoreError(f"invalid layers payload: {error}") from error


def _fields_payload(cell_set: CellSetState) -> dict[str, Any]:
    """Serialize the JSON half of a :class:`CellSetState` (no arrays)."""
    payload: dict[str, Any] = {
        "grid": {
            "dimension": int(cell_set.grid.dimension),
            "origin": [float(value) for value in cell_set.grid.origin],
            "spacing": float(cell_set.grid.spacing),
            "shape": [int(value) for value in cell_set.grid.shape],
        },
        "fields": {
            name: {
                "unit": value.unit,
                "association": value.association,
                "dtype": value.dtype,
                "shape": [int(entry) for entry in value.shape],
                "components": int(value.components),
            }
            for name, value in cell_set.fields.items()
        },
        "provenance": dict(cell_set.provenance),
    }
    if cell_set.grid.array_order != "C":
        payload["grid"]["arrayOrder"] = cell_set.grid.array_order
    if isinstance(cell_set.material_ids, np.ndarray):
        payload["materialIds"] = {
            "dtype": cell_set.material_ids.dtype.name,
            "shape": [int(value) for value in cell_set.material_ids.shape],
        }
    return payload


def _field_arrays(cell_set: CellSetState) -> dict[str, np.ndarray]:
    """Collect the ``fields.npz`` arrays for a :class:`CellSetState`."""
    arrays: dict[str, np.ndarray] = {}
    for index, (name, value) in enumerate(cell_set.fields.items()):
        if not isinstance(value.data, np.ndarray):
            raise StateStoreError(
                f"cell-set field {name!r} data is {type(value.data).__name__}, not a numpy array; "
                "opaque field handles cannot be persisted"
            )
        arrays[f"field_{index:03d}"] = value.data
    if cell_set.material_ids is not None:
        if not isinstance(cell_set.material_ids, np.ndarray):
            raise StateStoreError(
                f"cell-set material_ids is {type(cell_set.material_ids).__name__}, not a numpy array; "
                "opaque handles cannot be persisted"
            )
        arrays["material_ids"] = cell_set.material_ids
    return arrays


def _fields_from_payload(directory: Path, payload: Any) -> CellSetState | None:
    """Rebuild a :class:`CellSetState` (and its arrays) from one step directory."""
    if payload is None:
        return None
    try:
        grid_payload = payload["grid"]
        grid = GridSpec(
            dimension=grid_payload["dimension"],
            origin=tuple(grid_payload["origin"]),
            spacing=grid_payload["spacing"],
            shape=tuple(grid_payload["shape"]),
            array_order=grid_payload.get("arrayOrder", "C"),
        )
        definitions = payload.get("fields") or {}
        material_descriptor = payload.get("materialIds")
        arrays_omitted = bool(payload.get("arraysOmitted", False))
        arrays: dict[str, np.ndarray] = {}
        if not arrays_omitted and (definitions or material_descriptor is not None):
            arrays = _read_arrays(directory / FIELDS_FILE, what="cell-set arrays")

        fields: dict[str, Field] = {}
        for index, (name, specification) in enumerate(definitions.items()):
            key = f"field_{index:03d}"
            if not arrays_omitted and key not in arrays:
                raise StateStoreError(f"cell-set field {name!r} is missing array {key!r} in {FIELDS_FILE}")
            fields[name] = Field(
                name=name,
                unit=specification["unit"],
                association=specification["association"],
                dtype=specification["dtype"],
                shape=tuple(specification["shape"]),
                components=specification.get("components", 1),
                data=None if arrays_omitted else arrays[key],
            )

        material_ids: Any = None
        if material_descriptor is not None and not arrays_omitted:
            if "material_ids" not in arrays:
                raise StateStoreError(f"cell-set material_ids is missing from {FIELDS_FILE}")
            material_ids = arrays["material_ids"]

        provenance = dict(payload.get("provenance") or {})
        if arrays_omitted:
            provenance["arraysOmitted"] = True
        return CellSetState(
            grid=grid,
            material_ids=material_ids,
            fields=fields,
            provenance=provenance,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise StateStoreError(f"invalid fields payload: {error}") from error


def _outputs_payload(outputs: OutputSet) -> dict[str, Any]:
    """Serialize an :class:`OutputSet` (metrology records and artifacts).

    ``schemaVersion`` is the *storage* format version
    (:data:`OUTPUTS_SCHEMA_VERSION`), not ``OutputSet.schema_version``; it is
    deliberately independent of :data:`STATE_SCHEMA_VERSION`.
    """
    return {
        "schemaVersion": int(OUTPUTS_SCHEMA_VERSION),
        "metrology": [
            {
                "metric": record.metric,
                "value": float(record.value),
                "unit": record.unit,
                "region": record.region,
                "method": record.method,
                "uncertainty": None if record.uncertainty is None else float(record.uncertainty),
            }
            for record in outputs.metrology
        ],
        "artifacts": [
            {
                "kind": artifact.kind,
                "name": artifact.name,
                "path": artifact.path,
                "schemaVersion": int(artifact.schema_version),
                "metadata": dict(artifact.metadata),
            }
            for artifact in outputs.artifacts
        ],
    }


def _outputs_from_payload(payload: Any) -> OutputSet:
    """Rebuild an :class:`OutputSet` from ``outputs.json``."""
    if not isinstance(payload, dict):
        raise StateStoreError(f"outputs payload must be a JSON object, got {type(payload).__name__}")
    version = payload.get("schemaVersion")
    if version != OUTPUTS_SCHEMA_VERSION:
        raise StateStoreError(f"unsupported outputs schemaVersion {version!r}, expected {OUTPUTS_SCHEMA_VERSION}")
    try:
        return OutputSet(
            schema_version=version,
            metrology=tuple(
                MetrologyRecord(
                    metric=item["metric"],
                    value=item["value"],
                    unit=item["unit"],
                    region=item.get("region"),
                    method=item.get("method", "plugin"),
                    uncertainty=item.get("uncertainty"),
                )
                for item in payload.get("metrology") or ()
            ),
            artifacts=tuple(
                Artifact(
                    kind=item["kind"],
                    name=item["name"],
                    path=item.get("path"),
                    schema_version=item.get("schemaVersion", 1),
                    metadata=item.get("metadata") or {},
                )
                for item in payload.get("artifacts") or ()
            ),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise StateStoreError(f"invalid outputs payload: {error}") from error


class StateStore:
    """Read/write v2 runtime artifacts under one run directory."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """Return the run directory this store writes to."""
        return self._root

    def step_dir(self, step_id: str) -> Path:
        """Return ``<root>/steps/<step_id>`` (it may not exist yet)."""
        return self._root / "steps" / _validate_step_id(step_id)

    def save_state(
        self,
        step_id: str,
        state: ProcessState,
        *,
        geometry_ref: str | None = None,
        persist_fields: bool = True,
    ) -> dict[str, Any]:
        """Persist ``state`` for ``step_id`` and return the ``state.json`` payload.

        ``geometry_ref`` names the file (relative to the step directory) that
        holds the native level-set handle; the handle itself is not serialized.
        ``voxels`` are deliberately ignored. ``persist_fields=False`` writes the
        non-field state but omits the dense cell-set arrays and records
        ``fieldsOmitted: true``; the executor uses it for intermediate steps of
        a field sequence, which are not replay boundaries. The boundary step is
        written with the arrays once.
        """
        step_id = _validate_step_id(step_id)
        if geometry_ref is not None:
            if not isinstance(geometry_ref, str) or not geometry_ref.strip():
                raise StateStoreError(f"geometry_ref must be a non-empty string, got {geometry_ref!r}")
            if state.geometry is None:
                raise StateStoreError(f"step {step_id!r} has geometry_ref {geometry_ref!r} but no geometry state")

        fields_payload = None
        arrays: dict[str, np.ndarray] = {}
        fields_omitted = state.fields is not None and not persist_fields
        if state.fields is not None:
            fields_payload = _fields_payload(state.fields)
            if persist_fields:
                arrays = _field_arrays(state.fields)
            else:
                # Keep the grid/provenance so a reloaded state still knows the
                # cell set existed and which geometry it belongs to. The arrays
                # are deliberately absent; the next field step must fail closed
                # on a geometry digest mismatch instead of silently deriving a
                # fresh cell set.
                fields_payload["arraysOmitted"] = True

        payload = {
            "schemaVersion": STATE_SCHEMA_VERSION,
            "stepId": step_id,
            "materials": _materials_payload(state.materials),
            "geometry": _geometry_payload(state.geometry, geometry_ref),
            "layers": _layers_payload(state.layers),
            "fields": fields_payload,
        }
        if fields_omitted:
            payload["fieldsOmitted"] = True
        text = _dumps(payload, what=f"state for step {step_id!r}")

        directory = self.step_dir(step_id)
        directory.mkdir(parents=True, exist_ok=True)
        if arrays:
            _atomic_write_arrays(directory / FIELDS_FILE, arrays)
        elif fields_omitted:
            (directory / FIELDS_FILE).unlink(missing_ok=True)
        _atomic_write_text(directory / STATE_FILE, text)
        return payload

    def load_state(
        self,
        step_id: str,
        *,
        geometry_loader: _GEOMETRY_LOADER | None = None,
    ) -> ProcessState:
        """Rebuild a :class:`ProcessState` from ``state.json``.

        ``geometry_loader`` is called with the stored ``geometry.ref``; when a
        ref exists and no loader is supplied this raises :class:`StateStoreError`.
        """
        step_id = _validate_step_id(step_id)
        directory = self.step_dir(step_id)
        payload = _read_json(directory / STATE_FILE, what=f"state for step {step_id!r}")
        if not isinstance(payload, dict):
            raise StateStoreError(f"state payload must be a JSON object, got {type(payload).__name__}")
        version = payload.get("schemaVersion")
        if version != STATE_SCHEMA_VERSION:
            raise StateStoreError(f"unsupported state schemaVersion {version!r}, expected {STATE_SCHEMA_VERSION}")

        return ProcessState(
            geometry=self._geometry_from_payload(step_id, payload.get("geometry"), geometry_loader),
            materials=_materials_from_payload(payload.get("materials")),
            layers=_layers_from_payload(payload.get("layers")),
            fields=_fields_from_payload(directory, payload.get("fields")),
        )

    def save_outputs(self, step_id: str, outputs: OutputSet) -> dict[str, Any]:
        """Persist ``outputs`` for ``step_id`` and return the ``outputs.json`` payload.

        The written ``schemaVersion`` is :data:`OUTPUTS_SCHEMA_VERSION`; see
        :meth:`load_outputs` for the matching validation.
        """
        step_id = _validate_step_id(step_id)
        payload = _outputs_payload(outputs)
        text = _dumps(payload, what=f"outputs for step {step_id!r}")
        _atomic_write_text(self.step_dir(step_id) / OUTPUTS_FILE, text)
        return payload

    def load_outputs(self, step_id: str) -> OutputSet:
        """Rebuild an :class:`OutputSet` from ``outputs.json``.

        Rejects any file whose ``schemaVersion`` is not
        :data:`OUTPUTS_SCHEMA_VERSION`; this is unrelated to
        :data:`STATE_SCHEMA_VERSION`.
        """
        step_id = _validate_step_id(step_id)
        payload = _read_json(self.step_dir(step_id) / OUTPUTS_FILE, what=f"outputs for step {step_id!r}")
        return _outputs_from_payload(payload)

    def save_plan(self, step_id: str, plan: ExecutionPlan) -> dict[str, str]:
        """Persist ``plan.identity_json()`` as ``plan.json`` and return it."""
        step_id = _validate_step_id(step_id)
        identity = dict(plan.identity_json())
        text = _dumps(identity, what=f"plan identity for step {step_id!r}")
        _atomic_write_text(self.step_dir(step_id) / PLAN_FILE, text)
        return identity

    def save_flow(self, flow: dict[str, Any]) -> None:
        """Persist the caller-supplied run record as ``<root>/flow.json``."""
        if not isinstance(flow, dict):
            raise StateStoreError(f"flow must be a JSON object, got {type(flow).__name__}")
        text = _dumps(flow, what="flow record")
        _atomic_write_text(self._root / FLOW_FILE, text)

    def _geometry_from_payload(
        self,
        step_id: str,
        payload: Any,
        geometry_loader: _GEOMETRY_LOADER | None,
    ) -> LevelSetState | None:
        """Rebuild :class:`LevelSetState` when a ref and loader are available."""
        if payload is None:
            return None
        reference = payload.get("ref")
        if not reference:
            return None
        if geometry_loader is None:
            raise StateStoreError(
                f"step {step_id!r} references geometry {reference!r} but no geometry_loader was provided"
            )
        handle = geometry_loader(reference)
        if handle is None:
            raise StateStoreError(f"geometry_loader returned no handle for {reference!r} in step {step_id!r}")
        try:
            return LevelSetState(
                handle=handle,
                dimension=payload["dimension"],
                grid_delta=payload["gridDelta"],
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise StateStoreError(f"invalid geometry payload for step {step_id!r}: {error}") from error
