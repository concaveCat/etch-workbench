"""Native Faraday-cage ion-beam-etch variant.

The variant constructs the installed ``ps.FaradayCageParameters`` (an
``IBEParameters`` struct plus ``cageAngle``), wraps ``ps.FaradayCageEtching`` in
a ``ps.Process`` whose flux engine comes from the resolved execution profile,
and applies the same advection and ray parameters the worker uses. It never
imports the worker, :mod:`backend`, or a native library: ``ps`` always comes
from the engine lease.

The geometry mask list comes from
:func:`process_models.v2.engines.viennaps.geometry_mask_material_names`, the
same derivation the worker uses for the trench/hole/fin mask layer, stack mask
layers, and explicitly masked GDS layers. Passing the material handles to
``FaradayCageEtching`` keeps the mask materials at zero plane-wafer rate.

``FaradayCageEtching`` builds a periodic cage source and the native code warns
unless both lateral boundaries are periodic. The variant does not rewrite the
caller's domain; projects that run this model use periodic lateral boundaries.

The variant mutates the caller's level-set handle in place
(``mutates_input=True``), exactly like the worker path. The executor is
responsible for checkpointing before calling it.
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
    geometry_mask_material_names,
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

from ..params import COS4_FIELDS, IBE_SCALAR_FIELDS, FaradayCageParams

__all__ = ["FaradayCageNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")


class FaradayCageNativeVariant(EngineVariant):
    """Run one native ``ps.FaradayCageEtching`` step through the shared helpers.

    Reads native geometry and materials, writes the mutated geometry plus the
    canonical material order. ``supports_masks=True`` because the project
    geometry contributes mask materials to the native constructor.
    """

    variant_id = "native"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry", "materials"}),
        writes=frozenset({"geometry", "materials"}),
        supports_masks=True,
        experimental=True,
        mutates_input=True,
        determinism="strict",
        state_profile=StateProfile(allow_new_materials=False),
    )
    engine = EngineRequirement(
        kind="native",
        engine_id="viennaps",
        min_version="4.6.2",
        # 2D FaradayCageEtching only completes on the pinned CPU disk flux
        # engine; AUTO/GPU_DISK hangs for part of the cage/tilt matrix. 3D is
        # CPU-only for this plugin as well.
        profiles=(ExecutionProfile(backend="cpu", kernel="CPU_DISK"),),
    )
    implementation_hash: str | None = None
    implementation_files = (("process_models/v2/engines/viennaps.py", getattr(_engine_helpers, "__file__", "")),)

    def validate(self, ctx: ProcessContext, params: FaradayCageParams) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Field ranges, finiteness, the material-rate table, and
        ``thresholdEnergy < meanEnergy`` are already enforced by
        :class:`~plugins.faradayCage.params.FaradayCageParams`, so this hook has
        nothing left to add.
        """
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: FaradayCageParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one Faraday-cage step and return the committed state change.

        The native call sequence mirrors
        ``worker/specialized.py::build_specialized_model`` for ``faradayCage``
        followed by the worker's ``ps.Process`` setup: model, process, pinned
        flux engine, advection parameters, then ray parameters.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        parameters = ps.FaradayCageParameters()
        for name in IBE_SCALAR_FIELDS:
            setattr(parameters.ibeParams, name, float(getattr(params, name)))
        for index, name in enumerate(COS4_FIELDS, start=1):
            setattr(parameters.ibeParams.cos4Yield, f"a{index}", float(getattr(params, name)))
        for name, value in params.material_rates().items():
            parameters.ibeParams.materialPlaneWaferRate.set(material_handle(ps, name), float(value))
        parameters.cageAngle = float(params.cageAngle)

        mask_names = sorted(geometry_mask_material_names(project.get("geometry")))
        masks = [material_handle(ps, name) for name in mask_names]
        model = ps.FaradayCageEtching(parameters, masks)

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
                    "faradayCage requires ProcessContext.seed for a random-seed project; "
                    "the worker bridge passes the run seed"
                )
        rays, ray_actual = configure_rays(ps, project, seed)
        process.setParameters(rays)

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

        material_rates = params.material_rates()
        metadata: dict[str, Any] = {
            "nativeClass": "ps.FaradayCageEtching",
            "nativeParameterContainer": "ps.FaradayCageParameters",
            "operation": "etch",
            "parameters": params.scalar_values(),
            "cageAngle": float(parameters.cageAngle),
            "ibeParameters": params.ibe_values(),
            "cos4Yield": params.cos4_values(),
            "materialPlaneWaferRateByMaterial": material_rates,
            "maskMaterials": mask_names,
            "maskMaterialPlaneWaferRateForcedZero": True,
            "duration": float(params.duration),
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
            "faradayCage native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
