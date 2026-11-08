"""Thin per-operation variants backed by one shared :class:`GeometryEngine`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..contract import EngineVariant, Issue, ProcessContext, StateProfile, VariantCapabilities
from ..engine import EngineLease, EngineRequirement, ExecutionProfile
from ..state import ProcessState, StepResult
from .engine import GeometryEngine

__all__ = ["GeometryVariant"]

_PACKAGE = Path(__file__).resolve().parent


class GeometryVariant(EngineVariant):
    """One geometry operation, implemented by the shared ``GeometryEngine``.

    The engine requirement is the ordinary native ViennaPS lease. Geometry
    operations mutate the level-set domain in place, so the executor's normal
    ``mutates_input`` checkpoint path applies; they never read or write a
    cell set.
    """

    variant_id = "native"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry", "materials"}),
        writes=frozenset({"geometry", "materials"}),
        mutates_input=True,
        determinism="strict",
        state_profile=StateProfile(allow_new_materials=True),
    )
    engine = EngineRequirement(
        kind="native",
        engine_id="viennaps",
        min_version="4.6.2",
        profiles=(ExecutionProfile(backend="cpu", kernel="CPU_DISK"),),
    )
    implementation_hash: str | None = None
    implementation_files = (
        ("process_models/v2/geometry/engine.py", _PACKAGE / "engine.py"),
        ("process_models/v2/geometry/params.py", _PACKAGE / "params.py"),
    )

    def __init__(self, operation: str, engine: GeometryEngine | None = None) -> None:
        if operation not in GeometryEngine.capabilities:
            raise ValueError(f"unsupported geometry operation {operation!r}")
        self.operation = operation
        self._geometry = engine or GeometryEngine()

    def validate(self, ctx: ProcessContext, params: Any) -> list[Issue]:
        return self._geometry.validate(self.operation, ctx, params)

    def execute(
        self,
        state: ProcessState,
        params: Any,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        return self._geometry.execute(self.operation, state, params, ctx, engine)
