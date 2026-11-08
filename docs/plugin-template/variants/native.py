"""Template native variant: implement the contract, not a physics model yet."""

from __future__ import annotations

from pydantic import BaseModel

from process_models.v2.contract import (
    EngineVariant,
    Issue,
    ProcessContext,
    VariantCapabilities,
)
from process_models.v2.engine import EngineLease, EngineRequirement, ExecutionProfile
from process_models.v2.state import ProcessState, StateDelta, StepResult

__all__ = ["ExampleNativeVariant"]


class ExampleNativeVariant(EngineVariant):
    """A contract-clean variant whose ``execute`` currently changes nothing.

    Replace ``execute`` with the real simulation. ``engine.handle`` is the
    leased native module; write only changed flow state into ``StateDelta`` and
    put report data in ``outputs``/``metadata``/``warnings``.
    """

    variant_id = "native"
    version = "0.1.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry"}),
        writes=frozenset(),
        mutates_input=False,
        determinism="strict",
    )
    engine = EngineRequirement(
        kind="native",
        engine_id="viennaps",
        min_version="4.6.2",
        profiles=(ExecutionProfile(backend="cpu", kernel="CPU_DISK"),),
    )
    implementation_hash = None  # None: the registry hashes this package's files.

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: BaseModel,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        del state, params, ctx, engine
        return StepResult(
            delta=StateDelta(),
            metadata={"template": True},
        )
