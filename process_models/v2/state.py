"""Multi-representation process state and structured step outputs for v2.

v2.2 split
----------
Flow state and report data are separate:

* :class:`StateDelta` carries only what the *next* step consumes
  (``geometry``, ``layers``, ``fields``, ``voxels``).
* :class:`StepResult` pairs that delta with an :class:`OutputSet` (typed
  metrology records and artifacts), a small ``metadata`` mapping, and
  ``warnings``. Report data never counts as a state change.

Representations
---------------
* ``geometry`` carries an engine-specific level-set handle. Native variants
  may mutate that handle in place; the executor is responsible for
  checkpointing before calling variants that declare ``mutates_input``.
* ``layers`` is a typed :class:`LayerState` for processes that reason about a
  layer stack (for example a FinFET flow) instead of a single signed-distance
  field. Producers decide the layer order; the contract does not impose one.
* ``fields`` is a typed :class:`CellSetState`: a :class:`GridSpec`, per-cell
  material IDs, named scalar/vector/tensor :class:`Field` values, and an
  optional native handle. It is derived on demand for a contiguous
  ``implant -> anneal -> analysis`` sequence; geometry remains canonical.
* ``voxels`` is the dense height-field grid from :mod:`process_models.voxel`.
  Conversions in and out of voxels live in
  :class:`process_models.v2.adapters.DefaultStateAdapter`, never in a variant.
* ``materials`` is the single canonical description of the native material
  IDs in level-set order. It is always present (possibly with an empty
  ``order``) and is never duplicated on the level-set handle.

No backend, worker, or project-schema types appear here. ``ProcessState`` and
every nested value object are frozen; adapters build new states with
``dataclasses.replace`` and never mutate an input state. The engine lease is
passed to variants explicitly, not stored in the state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

import numpy as np

__all__ = [
    "Artifact",
    "ArtifactKind",
    "CellSetState",
    "Field",
    "FieldAssociation",
    "GridSpec",
    "Layer",
    "LayerState",
    "LevelSetState",
    "MaterialState",
    "MetrologyRecord",
    "OutputSet",
    "ProcessState",
    "StateDelta",
    "StateKind",
    "StateMissingError",
    "StepResult",
    "VoxelState",
]

StateKind = Literal["geometry", "materials", "layers", "fields", "voxels"]
FieldAssociation = Literal["cell", "point"]
ArtifactKind = Literal[
    "surface_mesh",
    "cell_set",
    "vtu",
    "json",
    "text",
    "native",
    "table",
    "profile",
    "device",
    "image",
]

#: Every representation :class:`ProcessState` can carry.
STATE_KINDS: frozenset[str] = frozenset({"geometry", "materials", "layers", "fields", "voxels"})
#: Every artifact kind :class:`Artifact` accepts.
ARTIFACT_KINDS: frozenset[str] = frozenset(
    {"surface_mesh", "cell_set", "vtu", "json", "text", "native", "table", "profile", "device", "image"}
)


class StateMissingError(RuntimeError):
    """Raised when a variant/request needs a state representation that is absent."""


def _non_empty_str(name: str, value: Any) -> str:
    """Return ``value`` as a non-empty string or raise ``ValueError``."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _index(value: Any, name: str) -> int:
    """Return ``value`` as a plain ``int`` or raise ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    return int(value)


def _positive_index(value: Any, name: str) -> int:
    """Return a strictly positive integer or raise ``ValueError``."""
    number = _index(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive, got {number!r}")
    return number


def _finite_float(name: str, value: Any) -> float:
    """Return ``value`` as a finite ``float`` or raise ``ValueError``."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return number


@dataclass(frozen=True)
class GridSpec:
    """Geometry of a regular cell grid.

    ``origin``/``shape`` have exactly ``dimension`` entries; ``spacing`` is the
    edge length of one cell and must be positive. ``array_order`` records how a
    flat native cell stream maps onto ``shape``: ``"C"`` makes the last axis
    fastest, ``"F"`` makes the first axis fastest. Native ViennaPS
    ``DenseCellSet`` streams are x-fastest, so a snapshot uses ``"F"`` with an
    ``(nx, ny, nz)`` shape; keeping the axes in x/y/z order makes
    ``data[i, j, k]`` address cell ``(i, j, k)`` directly.
    """

    dimension: int
    origin: tuple[float, ...]
    spacing: float
    shape: tuple[int, ...]
    array_order: Literal["C", "F"] = "C"

    def __post_init__(self) -> None:
        dimension = int(self.dimension)
        if dimension not in (2, 3):
            raise ValueError(f"dimension must be 2 or 3, got {self.dimension!r}")

        origin = tuple(_finite_float("grid origin", value) for value in self.origin)
        if len(origin) != dimension:
            raise ValueError(f"origin must have {dimension} entries, got {len(origin)}")

        spacing = _finite_float("grid spacing", self.spacing)
        if spacing <= 0.0:
            raise ValueError(f"grid spacing must be positive, got {self.spacing!r}")

        shape = tuple(_positive_index(value, "grid shape") for value in self.shape)
        if len(shape) != dimension:
            raise ValueError(f"shape must have {dimension} entries, got {len(shape)}")

        if self.array_order not in ("C", "F"):
            raise ValueError(f"array_order must be 'C' or 'F', got {self.array_order!r}")

        object.__setattr__(self, "dimension", dimension)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "spacing", spacing)
        object.__setattr__(self, "shape", shape)


@dataclass(frozen=True)
class Field:
    """One named field over a cell set.

    ``shape`` is the grid shape *without* the component axis. ``components``
    is ``1`` for scalar, ``3`` for vector, and ``6``/``9`` for tensors (other
    positive counts are allowed, but not interpreted).

    ``data`` may be a numpy array or an opaque handle. Numpy data must match
    ``dtype`` and either ``shape`` (scalar) or ``(components, *shape)``. Handle
    data is trusted: ``shape``/``dtype`` are then only the contract.
    """

    name: str
    unit: str
    association: FieldAssociation
    dtype: str
    shape: tuple[int, ...]
    components: int = 1
    data: Any = None

    def __post_init__(self) -> None:
        name = _non_empty_str("field name", self.name)
        unit = _non_empty_str("field unit", self.unit)
        if self.association not in ("cell", "point"):
            raise ValueError(f"field association must be 'cell' or 'point', got {self.association!r}")
        dtype = _non_empty_str("field dtype", self.dtype)
        try:
            numpy_dtype = np.dtype(dtype)
        except TypeError as error:
            raise ValueError(f"field dtype {dtype!r} is not a numpy dtype") from error

        shape = tuple(_positive_index(value, "field shape") for value in self.shape)
        components = _positive_index(self.components, "field components")

        if isinstance(self.data, np.ndarray):
            if self.data.dtype != numpy_dtype:
                raise ValueError(f"field {name!r} data dtype {self.data.dtype} does not match {numpy_dtype}")
            expected = (components, *shape) if components > 1 else shape
            if tuple(self.data.shape) != expected:
                raise ValueError(
                    f"field {name!r} data shape {tuple(self.data.shape)} does not match expected {expected}"
                )

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "dtype", dtype)
        object.__setattr__(self, "shape", shape)
        object.__setattr__(self, "components", components)


@dataclass(frozen=True)
class CellSetState:
    """A regular grid with per-cell material IDs and named cell fields.

    ``material_ids`` is a numpy array (checked for shape and integer dtype) or
    an opaque handle. ``fields`` is keyed by field name; every field must be
    cell-associated and cover the grid. ``provenance`` is informational only.

    ``handle`` is the engine's native cell-set object (for ViennaPS, a
    ``DenseCellSet``). It is authoritative while a contiguous field sequence is
    running; the arrays are a snapshot taken at sequence boundaries. A state
    loaded from disk has ``handle=None`` until a :class:`CellSetBackend`
    rehydrates it from the current geometry.

    ``domain_handle`` is the opaque engine domain that owns the native cell
    set. ViennaPS ``Process`` expects a ``Domain`` and looks up its cell set,
    so a field step needs the widened *copy* that carries the dense grid; the
    canonical ``ProcessState.geometry`` handle is never that copy.
    """

    grid: GridSpec
    material_ids: Any
    fields: Mapping[str, Field] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)
    handle: Any = field(default=None, compare=False, repr=False)
    domain_handle: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.grid, GridSpec):
            raise TypeError(f"grid must be a GridSpec, got {type(self.grid).__name__}")
        shape = tuple(self.grid.shape)

        fields: dict[str, Field] = {}
        for key, value in dict(self.fields).items():
            if not isinstance(value, Field):
                raise TypeError(f"cell-set field {key!r} must be a Field")
            if key != value.name:
                raise ValueError(f"cell-set field key {key!r} does not match field name {value.name!r}")
            if value.association != "cell":
                raise ValueError(f"cell-set field {key!r} must be cell-associated, got {value.association!r}")
            if tuple(value.shape) != shape:
                raise ValueError(f"cell-set field {key!r} shape {tuple(value.shape)} does not match grid {shape}")
            fields[key] = value

        if isinstance(self.material_ids, np.ndarray):
            if tuple(self.material_ids.shape) != shape:
                raise ValueError(f"material_ids shape {tuple(self.material_ids.shape)} does not match grid {shape}")
            if not np.issubdtype(self.material_ids.dtype, np.integer):
                raise ValueError(f"material_ids dtype must be integer, got {self.material_ids.dtype}")

        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "provenance", dict(self.provenance))


@dataclass(frozen=True)
class Layer:
    """One layer in a :class:`LayerState`.

    ``thickness`` is optional; when present it must be finite and positive.
    ``source_step`` records which step created the layer, if known.
    """

    layer_id: str
    material_id: int
    name: str
    source_step: str | None = None
    thickness: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        layer_id = _non_empty_str("layer_id", self.layer_id)
        material_id = _index(self.material_id, "layer material_id")
        if material_id < 0:
            raise ValueError(f"layer material_id must be >= 0, got {material_id}")
        name = _non_empty_str("layer name", self.name)
        source_step = self.source_step
        if source_step is not None:
            source_step = _non_empty_str("layer source_step", source_step)
        thickness = None
        if self.thickness is not None:
            thickness = _finite_float("layer thickness", self.thickness)
            if thickness <= 0.0:
                raise ValueError(f"layer thickness must be positive, got {self.thickness!r}")

        object.__setattr__(self, "layer_id", layer_id)
        object.__setattr__(self, "material_id", material_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "source_step", source_step)
        object.__setattr__(self, "thickness", thickness)
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True)
class LayerState:
    """An ordered tuple of :class:`Layer` objects with unique ``layer_id``.

    The contract does not impose a stack order yet. Producers document whether
    their order is top-to-bottom or bottom-to-top.
    """

    layers: tuple[Layer, ...]

    def __post_init__(self) -> None:
        layers = tuple(self.layers)
        for layer in layers:
            if not isinstance(layer, Layer):
                raise TypeError(f"layers must be Layer instances, got {type(layer).__name__}")
        identifiers = [layer.layer_id for layer in layers]
        duplicates = sorted({identifier for identifier in identifiers if identifiers.count(identifier) > 1})
        if duplicates:
            raise ValueError(f"layer ids must be unique, got duplicates {duplicates}")
        object.__setattr__(self, "layers", layers)


@dataclass(frozen=True)
class MetrologyRecord:
    """One measured or simulated metric reported by a step.

    ``value`` must be finite; ``uncertainty`` is optional and, when present,
    finite and non-negative.
    """

    metric: str
    value: float
    unit: str
    region: str | None = None
    method: str = "plugin"
    uncertainty: float | None = None

    def __post_init__(self) -> None:
        metric = _non_empty_str("metric", self.metric)
        value = _finite_float("metrology value", self.value)
        unit = _non_empty_str("metrology unit", self.unit)
        region = self.region
        if region is not None:
            region = _non_empty_str("metrology region", region)
        method = _non_empty_str("metrology method", self.method)
        uncertainty = None
        if self.uncertainty is not None:
            uncertainty = _finite_float("metrology uncertainty", self.uncertainty)
            if uncertainty < 0.0:
                raise ValueError(f"metrology uncertainty must be >= 0, got {self.uncertainty!r}")

        object.__setattr__(self, "metric", metric)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "uncertainty", uncertainty)


@dataclass(frozen=True)
class Artifact:
    """A named output produced by a step.

    ``path`` is optional so a variant can describe an in-memory artifact that
    the executor later persists. ``schema_version`` versions the artifact
    payload, not the Python contract.
    """

    kind: ArtifactKind
    name: str
    path: str | None = None
    schema_version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ARTIFACT_KINDS:
            raise ValueError(f"unknown artifact kind {self.kind!r}; expected one of {sorted(ARTIFACT_KINDS)}")
        name = _non_empty_str("artifact name", self.name)
        path = self.path
        if path is not None:
            path = _non_empty_str("artifact path", path)
        schema_version = _index(self.schema_version, "artifact schema_version")
        if schema_version < 1:
            raise ValueError(f"artifact schema_version must be >= 1, got {schema_version}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True)
class OutputSet:
    """Typed report data produced by one step.

    Metrology records and artifacts are *outputs*, never state: a step may
    report outputs without changing the flow state, and vice versa.
    """

    schema_version: int = 1
    metrology: tuple[MetrologyRecord, ...] = ()
    artifacts: tuple[Artifact, ...] = ()

    def __post_init__(self) -> None:
        schema_version = _index(self.schema_version, "output schema_version")
        if schema_version < 1:
            raise ValueError(f"output schema_version must be >= 1, got {schema_version}")
        metrology = tuple(self.metrology)
        for record in metrology:
            if not isinstance(record, MetrologyRecord):
                raise TypeError(f"metrology entries must be MetrologyRecord, got {type(record).__name__}")
        artifacts = tuple(self.artifacts)
        for artifact in artifacts:
            if not isinstance(artifact, Artifact):
                raise TypeError(f"artifacts must be Artifact, got {type(artifact).__name__}")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "metrology", metrology)
        object.__setattr__(self, "artifacts", artifacts)


@dataclass(frozen=True)
class MaterialState:
    """Native material IDs in level-set order plus optional human names.

    ``order`` has one entry per level-set index, so the **same native material
    ID may appear more than once** (for example two SiO2 depositions). This
    mirrors the native domain: multiple level sets can share a material ID.
    ``names`` is keyed by material ID, not by level-set index; names are
    unique. ``order`` may be empty so that field-only steps do not need a
    material map.
    """

    order: tuple[int, ...] = ()
    names: Mapping[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        order = tuple(int(material_id) for material_id in self.order)
        if any(material_id < 0 for material_id in order):
            raise ValueError(f"material ids must be >= 0, got {order}")

        names = {int(material_id): name for material_id, name in dict(self.names).items()}
        unknown = sorted(set(names) - set(order))
        if unknown:
            raise ValueError(f"material names reference ids outside order {order}: {unknown}")
        if any(not isinstance(name, str) or not name.strip() for name in names.values()):
            raise ValueError("material names must be non-empty strings")
        if len(set(names.values())) != len(names):
            raise ValueError(f"material names must be unique, got {sorted(names.values())}")

        object.__setattr__(self, "order", order)
        object.__setattr__(self, "names", names)

    def name_for(self, material_id: int) -> str | None:
        """Return the display name for ``material_id`` (or ``None``)."""
        return self.names.get(int(material_id))

    def id_for(self, name: str) -> int | None:
        """Return the material id registered under ``name`` (or ``None``)."""
        for material_id, candidate in self.names.items():
            if candidate == name:
                return material_id
        return None


@dataclass(frozen=True)
class LevelSetState:
    """An engine-specific level-set handle plus the grid metadata it carries.

    ``handle`` is opaque to this package. For native variants it is the
    ViennaPS ``Domain``; the :class:`process_models.v2.engine.EngineLease`
    handed to the variant provides the module/handle needed to operate on it.

    Material order is *not* stored here: :class:`MaterialState` on
    :class:`ProcessState` is the single canonical source.
    """

    handle: Any
    dimension: int
    grid_delta: float

    def __post_init__(self) -> None:
        if int(self.dimension) not in (2, 3):
            raise ValueError(f"dimension must be 2 or 3, got {self.dimension!r}")
        grid_delta = float(self.grid_delta)
        if not math.isfinite(grid_delta) or grid_delta <= 0.0:
            raise ValueError(f"grid_delta must be a positive finite number, got {self.grid_delta!r}")
        object.__setattr__(self, "dimension", int(self.dimension))
        object.__setattr__(self, "grid_delta", grid_delta)


@dataclass(frozen=True)
class VoxelState:
    """A dense material grid (``process_models.voxel.VoxelGrid``)."""

    grid: Any
    source: str = "height-field-v1"


@dataclass(frozen=True)
class ProcessState:
    """Everything a variant may read.

    The state is immutable. The engine lease is *not* part of it: the executor
    passes the acquired lease to :meth:`EngineVariant.execute` explicitly.
    """

    geometry: LevelSetState | None = None
    materials: MaterialState = field(default_factory=lambda: MaterialState(()))
    layers: LayerState | None = None
    fields: CellSetState | None = None
    voxels: VoxelState | None = None

    def has(self, kind: StateKind) -> bool:
        """Return whether ``kind`` is populated.

        ``materials`` is always present: :class:`ProcessState` carries a
        :class:`MaterialState` even when its ``order`` is empty (field-only
        steps).
        """
        if kind == "geometry":
            return self.geometry is not None
        if kind == "materials":
            return True
        if kind == "layers":
            return self.layers is not None
        if kind == "fields":
            return self.fields is not None
        if kind == "voxels":
            return self.voxels is not None
        raise ValueError(f"unknown state kind {kind!r}")

    def require(self, kind: StateKind) -> Any:
        """Return the representation for ``kind`` or raise :class:`StateMissingError`."""
        if not self.has(kind):
            raise StateMissingError(f"process state is missing {kind!r}")
        if kind == "geometry":
            return self.geometry
        if kind == "materials":
            return self.materials
        if kind == "layers":
            return self.layers
        if kind == "fields":
            return self.fields
        return self.voxels


@dataclass
class StateDelta:
    """The *changes* a variant wants committed.

    ``None`` means "unchanged" for every representation. Report data belongs
    in :class:`StepResult`, not here, so a step that only reports outputs
    returns an empty delta.
    """

    geometry: LevelSetState | None = None
    materials: MaterialState | None = None
    layers: LayerState | None = None
    fields: CellSetState | None = None
    voxels: VoxelState | None = None

    def is_empty(self) -> bool:
        """Return ``True`` when no state representation changes."""
        return (
            self.geometry is None
            and self.materials is None
            and self.layers is None
            and self.fields is None
            and self.voxels is None
        )


@dataclass
class StepResult:
    """One step's state change plus its report data.

    ``delta`` is committed by the adapter; ``outputs``, ``metadata``, and
    ``warnings`` are recorded by the executor and never touch the state.
    """

    delta: StateDelta
    outputs: OutputSet = field(default_factory=OutputSet)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
