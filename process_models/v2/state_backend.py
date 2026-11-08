"""Engine-neutral state access boundary.

``StateBackend`` is the engine's implementation of state *storage* and
*lifetime* operations: copy/checkpoint a native handle, write it to a
directory, load it back, and validate that the handle agrees with the
platform's canonical :class:`~process_models.v2.state.MaterialState`.

Representation conversion (voxels, cell sets, surface fields) is deliberately
not part of this protocol. Those belong to adapters/converters; a backend must
not become a god object that also owns process physics.

The default :class:`HandleStateBackend` is a compatibility adapter for engines
whose handle exposes ViennaPS-style ``Domain``/``Writer``/``Reader``
constructors. It keeps existing callers working while the executor stops
importing the ViennaPS module directly. A custom engine supplies its own
backend with its own handle type and serialization.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .materials import MaterialRegistry
from .state import STATE_KINDS, LevelSetState, ProcessState, StateKind

__all__ = [
    "GeometryRef",
    "HandleStateBackend",
    "StateBackend",
    "StateBackendError",
    "StateCapabilities",
]


class StateBackendError(RuntimeError):
    """Raised when an engine cannot copy, persist, or load its state."""


def _non_empty(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value


@dataclass(frozen=True)
class GeometryRef:
    """A persisted native geometry reference.

    ``path`` is the file name as written by the backend. ``backend_id`` and
    ``backend_version`` make the reference self-describing: a loader must never
    reinterpret it under a different backend.
    """

    path: str
    dimension: int
    grid_delta: float
    backend_id: str
    backend_version: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        path = _non_empty("geometry path", self.path)
        dimension = int(self.dimension)
        if dimension not in (2, 3):
            raise ValueError(f"geometry dimension must be 2 or 3, got {dimension!r}")
        grid_delta = float(self.grid_delta)
        if not grid_delta > 0.0:
            raise ValueError(f"geometry grid_delta must be positive, got {grid_delta!r}")
        _non_empty("geometry backend_id", self.backend_id)
        _non_empty("geometry backend_version", self.backend_version)
        schema_version = int(self.schema_version)
        if schema_version < 1:
            raise ValueError(f"geometry schema_version must be >= 1, got {schema_version!r}")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "dimension", dimension)
        object.__setattr__(self, "grid_delta", grid_delta)
        object.__setattr__(self, "schema_version", schema_version)


@dataclass(frozen=True)
class StateCapabilities:
    """What an engine's state backend can actually do.

    ``representations`` describes the in-memory kinds the backend can validate
    or copy. ``persistence`` is the subset it can write and load back.
    ``conversions`` names cross-representation operations the platform may ask
    this backend for; conversion implementations live outside the backend.
    """

    representations: frozenset[StateKind] = frozenset()
    persistence: frozenset[StateKind] = frozenset()
    conversions: frozenset[str] = frozenset()
    material_registry: bool = False
    mutation: bool = False

    def __post_init__(self) -> None:
        representations = frozenset(self.representations)
        persistence = frozenset(self.persistence)
        unknown = sorted((representations | persistence) - STATE_KINDS)
        if unknown:
            raise ValueError(f"unknown state kinds {unknown!r}; expected a subset of {sorted(STATE_KINDS)!r}")
        if not persistence <= representations:
            raise ValueError(
                f"persistence {sorted(persistence)!r} must be a subset of representations {sorted(representations)!r}"
            )
        conversions = frozenset(str(item) for item in self.conversions)
        if any(not item.strip() for item in conversions):
            raise ValueError(f"conversions must be non-empty strings, got {sorted(conversions)!r}")
        object.__setattr__(self, "representations", representations)
        object.__setattr__(self, "persistence", persistence)
        object.__setattr__(self, "conversions", conversions)


@runtime_checkable
class StateBackend(Protocol):
    """Engine-side native-state operations used by the executor."""

    id: str
    version: str
    capabilities: StateCapabilities
    material_registry: MaterialRegistry | None

    def checkpoint(self, state: ProcessState) -> ProcessState: ...

    def write(self, state: ProcessState, directory: Path) -> GeometryRef | None: ...

    def load(self, reference: GeometryRef, directory: Path | None = None) -> ProcessState: ...

    def validate(self, state: ProcessState) -> None: ...


@dataclass(frozen=True)
class HandleStateBackend:
    """Compatibility backend for a handle exposing ``Domain``/``Writer``/``Reader``.

    This is intentionally small. It is the *default* for ``StaticEngine`` so
    existing tests and callers keep their native handles, but the executor no
    longer knows about ``Domain`` or ``Writer``; it only calls this protocol.
    """

    handle: Any
    id: str = "handle-domain"
    version: str = "1"
    capabilities: StateCapabilities = StateCapabilities(
        representations=frozenset({"geometry"}),
        persistence=frozenset({"geometry"}),
        mutation=True,
    )
    domain_factory: Callable[[GeometryRef], Any] | None = None
    material_registry: MaterialRegistry | None = None

    def __post_init__(self) -> None:
        if self.material_registry is not None and not self.capabilities.material_registry:
            object.__setattr__(
                self,
                "capabilities",
                replace(self.capabilities, material_registry=True),
            )

    def checkpoint(self, state: ProcessState) -> ProcessState:
        geometry = state.geometry
        if geometry is None:
            return state
        domain_factory = getattr(self.handle, "Domain", None)
        if domain_factory is None or not callable(domain_factory):
            raise StateBackendError(
                f"state backend {self.id!r} cannot checkpoint a geometry handle: "
                "the engine exposes no Domain copy constructor"
            )
        copied = domain_factory(geometry.handle)
        if copied is None:
            raise StateBackendError(f"state backend {self.id!r} returned no copied geometry handle")
        return replace(state, geometry=replace(geometry, handle=copied), voxels=None)

    def write(self, state: ProcessState, directory: Path) -> GeometryRef | None:
        geometry = state.geometry
        if geometry is None:
            return None
        writer_factory = getattr(self.handle, "Writer", None)
        if writer_factory is None or not callable(writer_factory):
            return None
        path = Path(directory) / "geometry.vpsd"
        path.parent.mkdir(parents=True, exist_ok=True)
        writer_factory(geometry.handle, str(path)).apply()
        return GeometryRef(
            path=path.name,
            dimension=geometry.dimension,
            grid_delta=geometry.grid_delta,
            backend_id=self.id,
            backend_version=self.version,
        )

    def load(self, reference: GeometryRef, directory: Path | None = None) -> ProcessState:
        if reference.backend_id != self.id:
            raise StateBackendError(
                f"geometry {reference.path!r} was written by backend {reference.backend_id!r}, not {self.id!r}"
            )
        reader_factory = getattr(self.handle, "Reader", None)
        if reader_factory is None or not callable(reader_factory) or self.domain_factory is None:
            raise StateBackendError(
                f"state backend {self.id!r} cannot load geometry without a Reader and domain_factory"
            )
        path = Path(reference.path)
        if not path.is_absolute() and directory is not None:
            path = Path(directory) / path
        domain = self.domain_factory(reference)
        reader_factory(domain, str(path)).apply()
        return ProcessState(
            geometry=LevelSetState(
                handle=domain,
                dimension=reference.dimension,
                grid_delta=reference.grid_delta,
            )
        )

    def validate(self, state: ProcessState) -> None:
        geometry = state.geometry
        if geometry is None:
            return
        grid_delta = getattr(geometry.handle, "getGridDelta", None)
        if callable(grid_delta):
            observed = float(grid_delta())
            if abs(observed - float(geometry.grid_delta)) > 1e-12 * max(1.0, abs(observed)):
                raise StateBackendError(
                    f"geometry grid delta {observed!r} does not match state grid delta {geometry.grid_delta!r}"
                )
        mapping = getattr(geometry.handle, "getMaterialMap", None)
        if callable(mapping):
            material_map = mapping()
            observed_order = tuple(int(material_map.getMaterialIdAtIdx(index)) for index in range(material_map.size()))
            expected = tuple(int(material_id) for material_id in state.materials.order)
            if expected and observed_order != expected:
                raise StateBackendError(
                    f"geometry material order {observed_order!r} does not match MaterialState.order {expected!r}"
                )
