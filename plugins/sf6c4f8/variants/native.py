"""Native SF6/C4F8 plasma-etch variant.

The variant constructs the installed ``ps.SF6C4F8Etching`` through its flat
six-argument overload, wraps it in a pinned ``CPU_DISK`` ``ps.Process``, and
applies the same advection and coverage parameters the worker uses. It never
imports the worker, :mod:`backend`, or a native library: ``ps`` always comes
from the engine lease.

The variant mutates the caller's level-set handle in place
(``mutates_input=True``), exactly like the worker path. The executor is
responsible for checkpointing before calling it.

``nativeParameters.advanced`` and ``passivationFlux`` are deliberately out of
scope: steps that carry them are not claimed by this process (see
:mod:`plugins.sf6c4f8.step`) and keep the old worker parameter-object path.
``SF6C4F8Etching`` has no passivation particle, so a positive
``passivationFlux`` dereferences a missing native flux scalar and can SIGSEGV.
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
    configure_rays,
    material_map,
)
from process_models.v2.state import (
    LevelSetState,
    MaterialState,
    ProcessState,
    StateDelta,
    StepResult,
)

from ..params import FLAT_FIELDS, WORKBENCH_COVERAGE_TOLERANCE, SF6C4F8Params

__all__ = ["SF6C4F8NativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")


class SF6C4F8NativeVariant(EngineVariant):
    """Run one native ``ps.SF6C4F8Etching`` step through the shared helpers.

    Reads native geometry and materials, writes the mutated geometry plus the
    canonical material order. ``supports_masks=True`` because the native plasma
    model assigns its own per-material rates; no geometry mask list is passed.

    ``GPU_TRIANGLE`` is deliberately not declared: the worker path has no
    evidence that ``SF6C4F8Etching`` runs under that kernel, so the resolver
    must reject it instead of guessing.
    """

    variant_id = "native"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry", "materials"}),
        writes=frozenset({"geometry", "materials"}),
        supports_masks=True,
        mutates_input=True,
        determinism="strict",
        state_profile=StateProfile(allow_new_materials=False),
    )
    engine = EngineRequirement(
        kind="native",
        engine_id="viennaps",
        min_version="4.6.2",
        profiles=(
            ExecutionProfile(backend="cpu", kernel="CPU_DISK"),
            ExecutionProfile(backend="cpu", kernel="CPU_TRIANGLE"),
            ExecutionProfile(backend="gpu", kernel="GPU_DISK"),
        ),
    )
    implementation_hash: str | None = None
    implementation_files = (("process_models/v2/engines/viennaps.py", getattr(_engine_helpers, "__file__", "")),)

    def validate(self, ctx: ProcessContext, params: SF6C4F8Params) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Field ranges, finiteness, and ``coverageTolerance`` in ``[0, 1]`` are
        already enforced by :class:`~plugins.sf6c4f8.params.SF6C4F8Params`, so
        this hook has nothing left to add.
        """
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: SF6C4F8Params,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one SF6/C4F8 step and return the committed state change.

        The native call sequence is the flat-overload equivalent of
        ``worker/specialized.py::build_specialized_model`` followed by the
        worker's ``ps.Process`` setup: model, process, resolved flux engine,
        advection parameters, then coverage parameters.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        flat_values = params.flat_values()
        model = ps.SF6C4F8Etching(*(flat_values[name] for name in FLAT_FIELDS))
        process = ps.Process(domain, model, float(params.duration))
        execution = apply_execution_profile(ps, process, engine.profile)

        advection, advection_actual = configure_advection(ps, project)
        process.setParameters(advection)

        seed = ctx.seed
        if seed is None:
            random_settings = project.get("solver", {}).get("random") or {}
            if random_settings.get("mode") == "fixed":
                seed = int(random_settings.get("seed", 0))
            else:
                raise RuntimeError(
                    "sf6c4f8 requires ProcessContext.seed for a random-seed project; "
                    "the worker bridge passes the run seed"
                )
        rays, ray_actual = configure_rays(ps, project, seed)
        process.setParameters(rays)

        requested_tolerance = params.coverageTolerance
        # The worker has always pinned 1e-3 when a step omits the tolerance.
        # None means "not requested"; using the native default 0.0 here would
        # silently change the coverage iteration policy between both paths.
        coverage_tolerance = WORKBENCH_COVERAGE_TOLERANCE if requested_tolerance is None else float(requested_tolerance)
        coverage = ps.CoverageParameters()
        coverage.tolerance = coverage_tolerance
        process.setParameters(coverage)

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
            "nativeClass": "ps.SF6C4F8Etching",
            "nativeConstructor": "flat six-argument overload",
            "operation": "etch",
            "parameters": flat_values,
            "duration": float(params.duration),
            "coverageTolerance": coverage_tolerance,
            "coverageToleranceRequested": (None if requested_tolerance is None else float(requested_tolerance)),
            "coverageToleranceSource": ("workbench-default" if requested_tolerance is None else "param"),
            "timeStepRatio": float(advection.timeStepRatio),
            "timeStepRatioSource": advection_actual.get("timeStepRatioSource", "solver.timeStepRatio"),
            "fluxEngine": execution["kernel"],
            "executionProfile": execution,
            "rayTracing": ray_actual,
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
            "sf6c4f8 native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
