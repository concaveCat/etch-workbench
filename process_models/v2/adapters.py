"""State adapters between representations.

A variant declares what it reads and writes; the adapter is responsible for
making the declared read representation available and for committing the
returned :class:`~process_models.v2.state.StateDelta` back into a
:class:`~process_models.v2.state.ProcessState`.

Boundaries
----------
Only the default adapter exists. It converts height-field level-set geometry
to/from :class:`process_models.voxel.VoxelGrid` on demand. ``fields`` is derived
when the selected engine supplies a cell-set backend: the adapter calls
``ensure`` at the start of a field sequence and ``rehydrate`` for a snapshot
loaded from disk. ``layers`` remains stored-only.

The ViennaPS/viennals import lives behind :mod:`process_models.voxel` and is
performed lazily inside methods, so importing this package does not require
native libraries.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol, runtime_checkable

from .cellset import CellSetBackend, CellSetBackendError, CellSetConfig
from .contract import ProcessContext, VariantCapabilities
from .state import (
    LevelSetState,
    ProcessState,
    StateDelta,
    StateKind,
    StateMissingError,
    VoxelState,
)

__all__ = ["DefaultStateAdapter", "StateAdapter"]


@runtime_checkable
class StateAdapter(Protocol):
    """Converts state representations around one variant execution.

    ``provides`` lists the representations the adapter can materialize from an
    existing state (for example ``"voxels"`` from ``"geometry"``). The resolver
    uses it to admit derived reads without pretending every adapter can derive
    every representation.
    """

    id: str
    provides: frozenset[StateKind]

    def prepare(self, state: ProcessState, capabilities: VariantCapabilities) -> ProcessState: ...

    def commit(
        self,
        state: ProcessState,
        delta: StateDelta,
        capabilities: VariantCapabilities,
    ) -> ProcessState: ...


class DefaultStateAdapter:
    """The v2 default adapter.

    ``prepare`` materializes voxels from geometry when a variant reads them,
    derives or rehydrates fields when a cell-set backend is present, and raises
    :class:`StateMissingError` for missing ``layers`` (stored-only). ``commit``
    applies a geometry delta, otherwise writes a voxel delta back into a *new*
    geometry, then applies ``layers`` and ``fields`` deltas independently. The
    input state is never mutated.
    """

    id = "default-v2"
    #: Derived representations only. Geometry is passed through when present;
    #: it cannot be materialized from nothing. ``fields`` is derived when the
    #: selected engine supplies a :class:`CellSetBackend`; ``layers`` remains
    #: stored-only.
    provides = frozenset({"voxels"})

    def __init__(
        self,
        *,
        cellset_backend: CellSetBackend | None = None,
        context: ProcessContext | None = None,
        cellset_config: CellSetConfig | None = None,
    ) -> None:
        self._cellset_backend = cellset_backend
        self._context = context
        self._cellset_config = cellset_config
        self.provides = frozenset({"voxels"}) | ({"fields"} if cellset_backend is not None else set())

    def prepare(self, state: ProcessState, capabilities: VariantCapabilities) -> ProcessState:
        """Ensure every representation in ``capabilities.reads`` is available."""
        if "layers" in capabilities.reads and state.layers is None:
            raise StateMissingError("layer state is not available and cannot be derived")
        prepared = state
        if "fields" in capabilities.reads:
            prepared = self._prepare_fields(prepared)
        if "voxels" in capabilities.reads and prepared.voxels is None:
            if prepared.geometry is None:
                raise StateMissingError("voxel state requires geometry to voxelize")
            grid = _read_voxels(prepared.geometry.handle)
            prepared = replace(prepared, voxels=VoxelState(grid=grid))

        return prepared

    def _prepare_fields(self, state: ProcessState) -> ProcessState:
        """Derive, rehydrate, or validate the cell-set sidecar.

        Derivation is the only place a field sequence pays the native
        ``generateCellSet`` cost. A snapshot loaded from disk has no native
        handle and is rehydrated once; an in-memory sequence reuses the handle.
        A snapshot from a different geometry is refused instead of remapped.
        """
        backend = self._cellset_backend
        if backend is None:
            if state.fields is None:
                raise StateMissingError("field state is not available and cannot be derived")
            return state
        if state.geometry is None:
            raise StateMissingError("field state requires geometry to derive a cell set")

        context = self._context
        if context is None:
            if state.fields is None:
                raise StateMissingError("field state derivation requires a ProcessContext")
            return state

        config = self._cellset_config
        if config is None and state.fields is not None:
            config = CellSetConfig.from_state(state.fields)
        if config is None:
            raw = context.step.get("cellSet") if isinstance(context.step, Mapping) else None
            if raw is None:
                raise StateMissingError("field state derivation requires canonical cellSet settings on the step")
            try:
                config = CellSetConfig.from_mapping(raw)
            except CellSetBackendError as error:
                raise StateMissingError(str(error)) from error

        source_step_id = context.step.get("id") if isinstance(context.step, Mapping) else None
        try:
            if state.fields is None:
                fields = backend.ensure(
                    state.geometry,
                    context,
                    config,
                    source_step_id=str(source_step_id) if source_step_id is not None else None,
                )
                return replace(state, fields=fields)
            backend.validate(state.fields, context, config)
            digest = backend.geometry_digest(state.geometry)
            stored = state.fields.provenance.get("sourceGeometryDigest")
            if stored is not None and str(stored) != digest:
                raise StateMissingError(
                    "cell-set snapshot belongs to a different geometry "
                    f"(stored {stored}, current {digest}); this version does not remap fields "
                    "across a geometry change"
                )
            if state.fields.handle is None:
                fields = backend.rehydrate(state.geometry, state.fields, context, config)
                return replace(state, fields=fields)
            return state
        except CellSetBackendError as error:
            raise StateMissingError(str(error)) from error

    def commit(
        self,
        state: ProcessState,
        delta: StateDelta,
        capabilities: VariantCapabilities,
    ) -> ProcessState:
        """Return a new state with ``delta`` applied.

        Geometry wins over voxels when a variant returns both: a variant that
        produced a new native geometry has already superseded any voxel view.
        A committed geometry also drops a stale voxel snapshot so the next
        voxel reader re-derives it. ``materials`` is independent of geometry: a
        geometry delta never changes it, and a materials delta replaces it.
        ``layers`` and ``fields`` are independent of geometry/voxels and of
        each other.
        """
        prepared = state

        if delta.geometry is not None:
            if "geometry" not in capabilities.writes:
                raise _resolution_error(
                    "variant returned a geometry delta but does not declare 'geometry' in capabilities.writes"
                )
            prepared = replace(
                prepared,
                geometry=delta.geometry,
                voxels=None,
            )
        elif delta.voxels is not None:
            if "voxels" not in capabilities.writes:
                raise _resolution_error(
                    "variant returned a voxel delta but does not declare 'voxels' in capabilities.writes"
                )
            if state.geometry is None:
                raise StateMissingError("committing voxels requires the original geometry")

            new_handle = _write_voxels(state.geometry.handle, delta.voxels.grid)
            geometry = LevelSetState(
                handle=new_handle,
                dimension=state.geometry.dimension,
                grid_delta=state.geometry.grid_delta,
            )
            prepared = replace(
                prepared,
                geometry=geometry,
                voxels=None,
            )

        if delta.materials is not None:
            if "materials" not in capabilities.writes:
                raise _resolution_error(
                    "variant returned a materials delta but does not declare 'materials' in capabilities.writes"
                )
            profile = capabilities.state_profile
            if profile is not None and not profile.allow_new_materials:
                added = sorted(set(delta.materials.names.values()) - set(state.materials.names.values()))
                if added:
                    raise _resolution_error(
                        f"variant is not allowed to introduce materials {added!r}; "
                        "set StateProfile.allow_new_materials=True to permit material creation"
                    )
            prepared = replace(prepared, materials=delta.materials)

        if delta.layers is not None:
            if "layers" not in capabilities.writes:
                raise _resolution_error(
                    "variant returned a layers delta but does not declare 'layers' in capabilities.writes"
                )
            prepared = replace(prepared, layers=delta.layers)

        if delta.fields is not None:
            if "fields" not in capabilities.writes:
                raise _resolution_error(
                    "variant returned a fields delta but does not declare 'fields' in capabilities.writes"
                )
            prepared = replace(prepared, fields=delta.fields)

        if prepared is state:
            return state
        return prepared


def _resolution_error(message: str) -> Exception:
    """Build the resolver error lazily to avoid a circular import."""
    from .resolver import ExecutionResolutionError

    return ExecutionResolutionError(message)


def _read_voxels(handle: Any) -> Any:
    """Lazy indirection so core tests can run without native libraries."""
    from process_models.voxel import read_voxels

    return read_voxels(handle)


def _write_voxels(handle: Any, grid: Any) -> Any:
    """Lazy indirection so core tests can run without native libraries."""
    from process_models.voxel import write_voxels

    return write_voxels(handle, grid)
