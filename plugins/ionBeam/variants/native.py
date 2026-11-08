"""Native ion-beam etch (IBE) variant.

The variant constructs ``ps.IBEParameters`` from the flat catalog fields,
applies the ``cos4Yield`` coefficients and the per-material plane-wafer-rate
map, and passes the geometry mask materials to ``ps.IonBeamEtching``. It never
imports the worker, :mod:`backend`, or a native library: ``ps`` always comes
from the engine lease.

The call sequence matches
``worker/specialized.py::build_specialized_model`` for ``ionBeam`` followed by
the worker's ``ps.Process`` setup: model, process, resolved flux engine,
advection parameters, then ray tracing.

The variant mutates the caller's level-set handle in place
(``mutates_input=True``), exactly like the worker path. The executor is
responsible for checkpointing before calling it.

``nativeParameters.advanced`` is deliberately out of scope: steps that carry it
are not claimed by this process (see :mod:`plugins.ionBeam.step`) and keep the
old worker parameter-object path.
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

from ..params import IonBeamParams

__all__ = ["IonBeamNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")


class IonBeamNativeVariant(EngineVariant):
    """Run one native ``ps.IonBeamEtching`` step through the shared helpers.

    Reads native geometry and materials, writes the mutated geometry plus the
    canonical material order. ``supports_masks=True`` because the native model
    receives the geometry mask material handles and forces their plane wafer
    rate to zero.
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

    def validate(self, ctx: ProcessContext, params: IonBeamParams) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Field ranges, finiteness, and the material-rate table bounds are
        already enforced by :class:`~plugins.ionBeam.params.IonBeamParams`.
        The energy-order rule is repeated here as a defensive check: it must
        hold before ``IBEParameters`` is constructed, because the native
        normalization changes the flux sign at ``thresholdEnergy >=
        meanEnergy``.
        """
        del ctx
        if params.thresholdEnergy >= params.meanEnergy:
            return [
                Issue(
                    path="thresholdEnergy",
                    message=(
                        f"must be less than meanEnergy ({params.meanEnergy:g}); "
                        "thresholdEnergy >= meanEnergy reverses the native IonBeamEtching flux sign"
                    ),
                )
            ]
        return []

    def execute(
        self,
        state: ProcessState,
        params: IonBeamParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one IBE step and return the committed state change.

        The native call sequence is the parameter-object equivalent of
        ``worker/specialized.py::build_specialized_model`` followed by the
        worker's ``ps.Process`` setup: IBE parameters and masks, process,
        resolved flux engine, advection parameters, then ray tracing.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        parameters = ps.IBEParameters()
        scalar_values = params.native_scalar_values()
        for name, value in scalar_values.items():
            setattr(parameters, name, float(value))
        cos4_values = params.cos4_yield_values()
        for name, value in cos4_values.items():
            setattr(parameters.cos4Yield, name, float(value))
        material_rates = params.material_rates()
        for name, value in material_rates.items():
            parameters.materialPlaneWaferRate.set(material_handle(ps, name), float(value))

        mask_names = sorted(geometry_mask_material_names(project["geometry"]))
        masks = [material_handle(ps, name) for name in mask_names]
        model = ps.IonBeamEtching(parameters, masks)

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
                    "ionBeam requires ProcessContext.seed for a random-seed project; "
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

        metadata: dict[str, Any] = {
            "nativeClass": "ps.IonBeamEtching",
            "nativeConstructor": "IBEParameters + mask material list",
            "operation": "etch",
            "parameters": params.flat_values(),
            "materialPlaneWaferRateByMaterial": material_rates,
            "cos4Yield": cos4_values,
            "duration": float(params.duration),
            "maskMaterials": mask_names,
            "maskMaterialPlaneWaferRateForcedZero": True,
            "timeStepRatio": float(advection.timeStepRatio),
            "timeStepRatioSource": advection_actual.get("timeStepRatioSource", "solver.timeStepRatio"),
            "fluxEngine": execution["kernel"],
            "executionProfile": execution,
            "rayTracing": ray_actual,
            "rays": ray_actual,
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
            "ionBeam native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
