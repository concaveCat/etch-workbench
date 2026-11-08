"""Stable process identity and implementation registration for v2.

The v2 split mirrors the two questions a caller can ask:

* :class:`ProcessSpec` - *what is this process* (id, version, label, canonical
  parameter model, supported dimensions/operations).
* :class:`VariantRegistration` - *which implementation of it is available*,
  backed by an :class:`EngineRequirement`.

A project step references ``process_id`` and optionally ``variant_id``; it
never contains a module path or callable. :class:`ProcessRegistry` is the only
place that turns those IDs into an implementation.

Phase-1 boundaries
------------------
Fields, geometry constraints, and determinism are declarative metadata. The
resolver enforces reads/writes/dimensions; nothing else is a runtime guarantee.
No project JSON schema is imported here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Protocol

from pydantic import BaseModel

from .engine import EngineKind, EngineLease, EngineRequirement
from .state import STATE_KINDS, ProcessState, StateKind, StepResult

__all__ = [
    "EngineVariant",
    "Issue",
    "ModelDescriptor",
    "ProcessContext",
    "ProcessSpec",
    "StateProfile",
    "VariantCapabilities",
    "VariantDescriptor",
    "VariantRegistration",
]

Severity = Literal["error", "warning"]
Determinism = Literal["strict", "tolerance"]


@dataclass(frozen=True)
class Issue:
    """One validation finding.

    ``path`` points at the offending parameter (for example ``"thickness"``)
    so callers can attach the issue to the right field.
    """

    path: str
    message: str
    severity: Severity = "error"


@dataclass(frozen=True)
class ProcessContext:
    """Read-only project/step context used for validation.

    ``project`` and ``step`` are raw JSON mappings, not typed project models,
    so this package does not depend on the backend schema. ``previous_tops``
    carries the previous step's measured material tops when an operation
    derives a parameter from the incoming surface (material-aware CMP).
    """

    dimension: int
    grid_delta: float
    project: Mapping[str, Any] = field(default_factory=dict)
    step: Mapping[str, Any] = field(default_factory=dict)
    seed: int | None = None
    assets: Mapping[str, Any] = field(default_factory=dict)
    #: Measurements from the previous step, for operations that derive a
    #: parameter from the incoming surface (for example CMP stop material).
    previous_tops: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if int(self.dimension) not in (2, 3):
            raise ValueError(f"dimension must be 2 or 3, got {self.dimension!r}")
        grid_delta = float(self.grid_delta)
        if not math.isfinite(grid_delta) or grid_delta <= 0.0:
            raise ValueError(f"grid_delta must be a positive finite number, got {self.grid_delta!r}")
        if self.seed is not None:
            if isinstance(self.seed, bool) or not isinstance(self.seed, int):
                raise ValueError(f"seed must be an integer or None, got {self.seed!r}")
        object.__setattr__(self, "dimension", int(self.dimension))
        object.__setattr__(self, "grid_delta", grid_delta)
        object.__setattr__(self, "seed", int(self.seed) if self.seed is not None else None)
        object.__setattr__(self, "assets", dict(self.assets))
        object.__setattr__(self, "previous_tops", dict(self.previous_tops))


@dataclass(frozen=True)
class StateProfile:
    """Declarative state requirements beyond ``reads``/``writes``.

    ``reads``/``writes`` answer *which* representations a variant uses. This
    profile answers *how* it may use them: which conversions are allowed, which
    materials must exist, whether a step may introduce materials, and which
    outputs are intended to be replayable. The resolver materializes a concrete
    :class:`StateContract` from this profile and the selected state backend.
    """

    conversions: tuple[str, ...] = ()
    required_materials: tuple[str, ...] = ()
    allow_new_materials: bool = False
    persistence: frozenset[StateKind] = frozenset()

    def __post_init__(self) -> None:
        conversions = tuple(str(conversion) for conversion in self.conversions)
        if any(not conversion.strip() for conversion in conversions):
            raise ValueError(f"state conversions must be non-empty strings, got {conversions!r}")
        required_materials = tuple(str(name) for name in self.required_materials)
        if any(not name.strip() for name in required_materials):
            raise ValueError(f"required material names must be non-empty strings, got {required_materials!r}")
        persistence = frozenset(self.persistence)
        unknown = sorted(persistence - STATE_KINDS)
        if unknown:
            raise ValueError(f"unknown persistence state kinds {unknown!r}")
        object.__setattr__(self, "conversions", conversions)
        object.__setattr__(self, "required_materials", required_materials)
        object.__setattr__(self, "persistence", persistence)


@dataclass(frozen=True)
class VariantCapabilities:
    """Declarative limits of a variant.

    ``reads``/``writes`` drive the resolver's state checks and the adapter's
    prepare/commit behaviour. ``geometry_constraints`` is descriptive only in
    phase 1 (for example ``("height-field",)``). ``output_kinds`` is a
    declarative hint of the structured outputs a variant can emit (for example
    ``("metrology", "artifact:profile")``); the executor records it but does not
    enforce it in this slice.
    """

    dimensions: tuple[int, ...]
    reads: frozenset[StateKind]
    writes: frozenset[StateKind]
    geometry_constraints: tuple[str, ...] = ()
    supports_fields: bool = False
    supports_masks: bool = False
    experimental: bool = False
    mutates_input: bool = False
    determinism: Determinism = "strict"
    output_kinds: tuple[str, ...] = ()
    state_profile: StateProfile | None = None

    def __post_init__(self) -> None:
        dimensions = tuple(int(dimension) for dimension in self.dimensions)
        if not dimensions:
            raise ValueError("dimensions must not be empty")
        if any(dimension not in (2, 3) for dimension in dimensions):
            raise ValueError(f"dimensions must be a subset of (2, 3), got {dimensions!r}")
        if self.determinism not in ("strict", "tolerance"):
            raise ValueError(f"unknown determinism {self.determinism!r}")
        reads = frozenset(self.reads)
        writes = frozenset(self.writes)
        unknown = sorted((reads | writes) - STATE_KINDS)
        if unknown:
            raise ValueError(f"unknown state kinds {unknown!r}; expected a subset of {sorted(STATE_KINDS)!r}")
        output_kinds = tuple(self.output_kinds)
        if any(not isinstance(kind, str) or not kind.strip() for kind in output_kinds):
            raise ValueError(f"output_kinds must be non-empty strings, got {output_kinds!r}")
        object.__setattr__(self, "dimensions", dimensions)
        object.__setattr__(self, "reads", reads)
        object.__setattr__(self, "writes", writes)
        state_profile = self.state_profile
        if state_profile is not None and not isinstance(state_profile, StateProfile):
            raise TypeError(f"state_profile must be a StateProfile or None, got {type(state_profile).__name__}")
        object.__setattr__(self, "geometry_constraints", tuple(self.geometry_constraints))
        object.__setattr__(self, "output_kinds", output_kinds)


class EngineVariant(Protocol):
    """One swappable implementation of a process.

    Identity, capabilities, and the engine requirement live on the variant
    itself; :class:`VariantRegistration` derives them.
    """

    variant_id: str
    version: str
    capabilities: VariantCapabilities
    engine: EngineRequirement
    implementation_hash: str | None

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        """Return issues for ``params`` in ``ctx``; empty means valid."""

    def execute(
        self,
        state: ProcessState,
        params: BaseModel,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Run one step with ``engine`` and return a :class:`StepResult`."""


@dataclass(frozen=True)
class ProcessSpec:
    """Stable process identity and its canonical parameter model.

    Multiple versions of the same ``id`` may be registered at once. Variants
    are registered separately and are not tied to a single process version.

    The optional ``parse_step`` hook is the single runtime dispatch helper for
    worker steps: it returns the canonical parameter model when a raw step
    belongs to this process, or ``None`` when it does not. Malformed fields may
    raise ``ValueError``. It is a callable, so it is deliberately excluded from
    :class:`ModelDescriptor` (the serializable description).
    ``default_variant`` names the variant to use when a step does not pin one.
    """

    id: str
    version: str
    label: str
    params_model: type[BaseModel]
    dimensions: tuple[int, ...]
    operations: tuple[str, ...]
    description: str = ""
    default_variant: str | None = None
    execution_kind: Literal["process", "field", "geometry"] = "process"
    parse_step: Callable[[Mapping[str, Any], Mapping[str, Any]], BaseModel | None] | None = None

    def __post_init__(self) -> None:
        for name in ("id", "version", "label"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not (isinstance(self.params_model, type) and issubclass(self.params_model, BaseModel)):
            raise TypeError("params_model must be a pydantic BaseModel subclass")
        dimensions = tuple(int(dimension) for dimension in self.dimensions)
        if not dimensions or any(dimension not in (2, 3) for dimension in dimensions):
            raise ValueError(f"dimensions must be a non-empty subset of (2, 3), got {dimensions!r}")
        operations = tuple(str(operation) for operation in self.operations)
        if not operations:
            raise ValueError("operations must not be empty")
        if self.default_variant is not None and (
            not isinstance(self.default_variant, str) or not self.default_variant.strip()
        ):
            raise ValueError("default_variant must be a non-empty string or None")
        if self.parse_step is not None and not callable(self.parse_step):
            raise TypeError("parse_step must be callable or None")
        if self.execution_kind not in ("process", "field", "geometry"):
            raise ValueError(f"execution_kind must be 'process', 'field', or 'geometry', got {self.execution_kind!r}")
        object.__setattr__(self, "dimensions", dimensions)
        object.__setattr__(self, "operations", operations)


@dataclass(frozen=True)
class VariantRegistration:
    """One implementation of a process.

    The registration is just ``(process_id, variant)``: ``variant_id``,
    ``version``, ``capabilities``, ``engine``, and the declared
    ``implementation_hash`` are read from the variant itself.
    """

    process_id: str
    variant: EngineVariant

    def __post_init__(self) -> None:
        if not isinstance(self.process_id, str) or not self.process_id.strip():
            raise ValueError("process_id must be a non-empty string")
        for name in ("variant_id", "version"):
            value = getattr(self.variant, name, None)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"variant {name} must be a non-empty string")
        if not isinstance(getattr(self.variant, "capabilities", None), VariantCapabilities):
            raise TypeError("variant capabilities must be a VariantCapabilities")
        if not isinstance(getattr(self.variant, "engine", None), EngineRequirement):
            raise TypeError("variant engine must be an EngineRequirement")
        for name in ("validate", "execute"):
            if not callable(getattr(self.variant, name, None)):
                raise TypeError(f"variant {name} must be callable")
        implementation_hash = getattr(self.variant, "implementation_hash", None)
        if implementation_hash is not None and not isinstance(implementation_hash, str):
            raise TypeError("variant implementation_hash must be a string or None")

    @property
    def variant_id(self) -> str:
        return self.variant.variant_id

    @property
    def version(self) -> str:
        return self.variant.version

    @property
    def capabilities(self) -> VariantCapabilities:
        return self.variant.capabilities

    @property
    def engine(self) -> EngineRequirement:
        return self.variant.engine

    @property
    def declared_hash(self) -> str | None:
        return self.variant.implementation_hash


@dataclass(frozen=True)
class VariantDescriptor:
    """Public, serializable description of one registered variant."""

    id: str
    version: str
    kind: EngineKind
    engine_id: str
    reads: tuple[StateKind, ...]
    writes: tuple[StateKind, ...]
    experimental: bool
    implementation_hash: str
    output_kinds: tuple[str, ...] = ()
    execution_profiles: tuple[str, ...] = ()
    state_profile: dict[str, object] | None = None


@dataclass(frozen=True)
class ModelDescriptor:
    """Public, serializable description of one process version.

    Descriptors never contain module paths or callables.
    """

    id: str
    version: str
    label: str
    dimensions: tuple[int, ...]
    operations: tuple[str, ...]
    variants: tuple[VariantDescriptor, ...]
    description: str = ""
    execution_kind: Literal["process", "field", "geometry"] = "process"
