"""Bridge between the worker's live Domain and the v2 ProcessState.

The legacy worker owns ``ctx.domain`` and ``ctx.field_state``.  The opt-in v2
execution path keeps a ``ProcessState`` in ``ctx.v2_state`` so process and
geometry steps share material order and (when present) the field sidecar.
Any legacy or no-op step that mutates the Domain invalidates the cached state;
the next v2 step rebuilds it from the live Domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from process_models.v2.engines.viennaps import material_map
from process_models.v2.state import LevelSetState, MaterialState, ProcessState

__all__ = ["WorkerV2State"]


@dataclass
class WorkerV2State:
    """One persistent v2 state anchored to the worker's current Domain."""

    state: ProcessState

    @classmethod
    def from_context(cls, ctx: Any) -> "WorkerV2State":
        previous = getattr(ctx, "v2_state", None)
        if (
            isinstance(previous, ProcessState)
            and previous.geometry is not None
            and previous.geometry.handle is ctx.domain
        ):
            # The committed v2 state is authoritative for any engine handle
            # type. Do not interrogate a custom handle with ViennaPS-only
            # helpers on every step.
            return cls(previous)
        geometry = LevelSetState(
            handle=ctx.domain,
            dimension=int(ctx.project["dimension"]),
            grid_delta=float(ctx.project["domain"]["gridDelta"]),
        )
        if hasattr(ctx.domain, "getMaterialMap"):
            order = tuple(int(material_id) for material_id in material_map(ctx.domain))
            names: dict[int, str] = {}
        else:
            # A custom engine may expose no ViennaPS material map. Its variant
            # can still start from an empty material state; a geometry/materials
            # delta commits the authoritative order.
            order = ()
            names = {}
        fields = None
        return cls(
            ProcessState(
                geometry=geometry,
                materials=MaterialState(order=order, names=names),
                fields=fields,
            )
        )

    def commit(self, ctx: Any, state: ProcessState) -> None:
        """Publish the committed v2 state back to the worker context."""
        if state.geometry is None:
            raise RuntimeError("v2 execution committed a state without geometry")
        ctx.domain = state.geometry.handle
        ctx.v2_state = state

    @staticmethod
    def invalidate(ctx: Any) -> None:
        """Drop the cached state after a legacy step mutates the Domain.

        If the dropped state carried field data, remember that the field
        history was invalidated. A later v2 field step must fail closed instead
        of silently deriving a fresh cell set.
        """
        previous = getattr(ctx, "v2_state", None)
        if isinstance(previous, ProcessState) and previous.fields is not None:
            ctx.field_state["v2FieldInvalidated"] = True
        ctx.v2_state = None
