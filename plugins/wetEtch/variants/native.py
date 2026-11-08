"""Native crystalline wet-etch variant.

The variant constructs the installed ``ps.WetEtching`` through either overload
used by ``worker/specialized.py::build_specialized_model``:

* ``ps.WetEtching([(material_handle, rate), ...])`` for isotropic material
  rates, or
* ``ps.WetEtching(direction100, direction010, rate100, rate110, rate111,
  rate311, rows)`` when ``useCrystallineRates`` is true.

Rows are always sorted by material name. The model is wrapped in a
``ps.Process`` whose flux engine is pinned to ``CPU_DISK`` by the resolved
execution profile. No flux is calculated and no ray parameters are configured,
but the engine pin is still required: without it the native process may
initialize the CUDA code path even though this model never uses it.

The variant mutates the caller's level-set handle in place
(``mutates_input=True``), exactly like the worker path. The executor is
responsible for checkpointing before calling it.

The variant never imports the worker, :mod:`backend`, or a native library;
``ps`` always comes from the engine lease.
"""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import (
    EngineVariant,
    Issue,
    ProcessContext,
    StateProfile,
    VariantCapabilities,
)
from process_models.v2.engine import EngineLease, EngineRequirement, ExecutionProfile
from process_models.v2.engines import viennaps as _engine_helpers
from process_models.v2.engines.viennaps import (
    apply_execution_profile,
    configure_advection,
    material_handle,
    material_map,
)
from process_models.v2.state import (
    LevelSetState,
    MaterialState,
    ProcessState,
    StateDelta,
    StepResult,
)

from ..params import WetEtchParams

__all__ = ["WetEtchNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")


class WetEtchNativeVariant(EngineVariant):
    """Run one native ``ps.WetEtching`` step through the shared helpers.

    Reads native geometry and materials, writes the mutated geometry plus the
    canonical material order. ``supports_masks=False`` because the native wet
    model takes its per-material rates from ``materialRates``; there is no
    separate geometry mask list.
    """

    variant_id = "native"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry", "materials"}),
        writes=frozenset({"geometry", "materials"}),
        supports_masks=False,
        mutates_input=True,
        determinism="strict",
        state_profile=StateProfile(allow_new_materials=False),
    )
    engine = EngineRequirement(
        kind="native",
        engine_id="viennaps",
        min_version="4.6.2",
        profiles=(ExecutionProfile(backend="cpu", kernel="CPU_DISK"),),
    )
    implementation_hash: str | None = None
    implementation_files = (("process_models/v2/engines/viennaps.py", getattr(_engine_helpers, "__file__", "")),)

    def validate(self, ctx: ProcessContext, params: WetEtchParams) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Field ranges, finiteness, non-zero directions, and the non-negative
        material rates are already enforced by
        :class:`~plugins.wetEtch.params.WetEtchParams`. The one remaining rule
        is that an execution needs at least one material row; a parse-accepted
        step always has one, but a directly constructed params object may not.
        """
        del ctx
        if not params.materialRates:
            return [
                Issue(
                    path="materialRates",
                    message="wetEtch requires at least one material rate row",
                )
            ]
        return []

    def execute(
        self,
        state: ProcessState,
        params: WetEtchParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one wet-etch step and return the committed state change.

        The native call sequence is the flat-overload equivalent of
        ``worker/specialized.py::build_specialized_model`` followed by the
        worker's ``ps.Process`` setup: model, process, pinned CPU engine,
        advection parameters, then ``apply``. Ray tracing is deliberately
        absent because ``WetEtching`` does not consume a flux field.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        if not params.materialRates:
            raise ValueError("wetEtch requires at least one material rate row")

        rows = [(material_handle(ps, name), float(rate)) for name, rate in params.sorted_material_rates()]
        if params.useCrystallineRates:
            model = ps.WetEtching(
                list(params.direction100),
                list(params.direction010),
                float(params.rate100),
                float(params.rate110),
                float(params.rate111),
                float(params.rate311),
                rows,
            )
            overload = "crystalline"
            rate_mode = "crystalline"
        else:
            model = ps.WetEtching(rows)
            overload = "materialRates"
            rate_mode = "material"

        process = ps.Process(domain, model, float(params.duration))
        execution = apply_execution_profile(ps, process, engine.profile)

        advection, advection_actual = configure_advection(ps, project)
        process.setParameters(advection)

        process.apply()

        material_order = tuple(int(material_id) for material_id in material_map(domain))
        delta_geometry = LevelSetState(
            handle=domain,
            dimension=geometry.dimension,
            grid_delta=geometry.grid_delta,
        )
        delta_materials = MaterialState(
            order=material_order,
            names={
                material_id: name
                for material_id, name in state.materials.names.items()
                if material_id in material_order
            },
        )

        metadata: dict[str, Any] = {
            "nativeClass": "ps.WetEtching",
            "overload": overload,
            "nativeConstructor": (
                "crystalline seven-argument overload" if params.useCrystallineRates else "materialRates overload"
            ),
            "rateMode": rate_mode,
            "operation": "etch",
            "parameters": params.native_parameters(),
            "duration": float(params.duration),
            "timeStepRatio": float(advection.timeStepRatio),
            "timeStepRatioSource": advection_actual.get("timeStepRatioSource", "solver.timeStepRatio"),
            "fluxEngine": execution["kernel"],
            "executionProfile": execution,
            "dimension": geometry.dimension,
            "gridDelta": geometry.grid_delta,
            "levelSetCount": int(domain.getNumberOfLevelSets()),
            "materialMap": list(material_order),
        }

        return StepResult(
            delta=StateDelta(geometry=delta_geometry, materials=delta_materials),
            metadata=metadata,
            warnings=[],
        )


def _require_project(ctx: ProcessContext) -> Any:
    """Return ``ctx.project`` or raise when a required key is missing.

    The shared advection helper reads the project schema version and the solver
    block. Silently substituting defaults would make the run record describe a
    project that was never used, so a partial context fails loudly.
    """
    project = ctx.project
    missing = [name for name in _REQUIRED_PROJECT_KEYS if not project.get(name)]
    if missing:
        raise RuntimeError(
            "wetEtch native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
