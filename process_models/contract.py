"""Phase-1 process-model plugin contract.

Scope
-----
This module defines two stable levels:

* :class:`ProcessPlugin` - the *process identity* plus the canonical Pydantic
  parameter model (``params_model``). A project step refers to a process by
  its ``id`` only.
* :class:`EngineVariant` - one swappable implementation of that process
  (native, surrogate, external, ...). A project step may pin a ``variant_id``
  and version, but never a Python module, path, or callable.

The registry in :mod:`process_models.registry` is the only place that turns
those stable IDs into an implementation.

Non-goals (phase 1)
-------------------
* Native level-set state only. :class:`ProcessState` carries the ViennaPS
  module handle, the level-set domain, and the solver context. Voxel
  conversion is a future *per-step adapter*; no voxel types appear here.
  ``ProcessState.fields`` is the reserved extension point for that adapter.
* No execution engine, scheduling, persistence, or project-JSON changes.
  This module only describes and validates the boundary.
* No plugin discovery or dynamic imports. Registration is explicit.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["error", "warning"]


class Issue(BaseModel):
    """A single validation finding produced by a variant.

    ``path`` points at the offending parameter (for example ``"thickness"``)
    so callers can attach the issue to the right field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = ""
    message: str
    severity: Severity = "error"


class ProcessContext(BaseModel):
    """Read-only project/step context that selected a variant.

    ``project`` and ``step`` are the raw JSON dictionaries, not typed project
    models, so this package does not depend on the backend schema. The
    context is the only project data a variant may read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: Literal[2, 3]
    grid_delta: float = Field(gt=0)
    project: dict[str, Any] = Field(default_factory=dict)
    step: dict[str, Any] = Field(default_factory=dict)
    materials: dict[str, Any] = Field(default_factory=dict)


class ModelDescriptor(BaseModel):
    """Public, serializable description of one plugin version.

    Descriptors are what a UI or API lists. They never contain module paths,
    callables, or other implementation details.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    label: str
    version: str
    kind: str
    operation: str
    dimensions: list[int] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    notes: str = ""


class VariantCapabilities(BaseModel):
    """Declared limits of a variant, used for admission checks.

    Capabilities are declarative metadata only; the registry does not enforce
    them. A future planner can reject a request whose step exceeds them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimensions: list[int] = Field(default_factory=list)
    engines: list[str] = Field(default_factory=list)
    supports_fields: bool = False
    supports_masks: bool = False
    experimental: bool = False


@dataclass
class ProcessState:
    """Mutable native level-set state handed to a variant.

    ``ps`` is the ViennaPS module (or a compatible facade), ``domain`` the
    level-set domain owned by the caller. A variant mutates ``domain`` in
    place and returns it inside :class:`ProcessResult`.

    ``fields`` is the phase-1 extension point for future per-step adapters
    (for example voxel data). No voxel types are defined yet.
    """

    ps: Any
    domain: Any
    dimension: int
    grid_delta: float
    materials: dict[str, Any] = field(default_factory=dict)
    seed: int = 0
    run_dir: Path | None = None
    logger: Any = None
    fields: dict[str, Any] | None = None


@dataclass
class ProcessResult:
    """Result of executing one step.

    ``metadata`` carries free-form, JSON-serializable bookkeeping (rates,
    effective parameters, ...), ``outputs`` names the produced artifacts, and
    ``warnings`` carries non-fatal messages for the run log.
    """

    domain: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class EngineVariant(ABC):
    """One swappable implementation of a process.

    Subclasses set ``variant_id`` and ``version`` to stable identifiers
    (never module paths), and may override ``capabilities`` and
    ``implementation_hash``. The default ``implementation_hash`` is derived
    by the registry from the variant class source.

    A variant receives the plugin's validated parameter model instance as
    ``params``; it must not re-validate the whole project.
    """

    variant_id: str
    version: str
    capabilities: VariantCapabilities = VariantCapabilities()
    implementation_hash: str | None = None

    @abstractmethod
    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        """Return issues for ``params`` in ``ctx``; an empty list means valid."""

    @abstractmethod
    def execute(self, state: ProcessState, params: BaseModel, ctx: ProcessContext) -> ProcessResult:
        """Run one step against the native level-set ``state``."""


class ProcessPlugin(ABC):
    """Stable process identity plus its canonical parameter model.

    ``params_model`` is the single Pydantic model every variant of this
    process accepts. ``variants`` holds the concrete implementations; the
    registry rejects duplicate ``variant_id`` values within one plugin
    version.
    """

    id: str
    version: str
    params_model: type[BaseModel]
    variants: Sequence[EngineVariant]

    @abstractmethod
    def describe(self) -> ModelDescriptor:
        """Return the public descriptor for this exact plugin version.

        ``descriptor.id`` and ``descriptor.version`` must match the plugin's
        own ``id`` and ``version``; the registry enforces this.
        """
