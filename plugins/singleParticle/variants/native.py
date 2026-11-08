"""Native single-particle plasma variant.

The variant constructs the installed ``ps.SingleParticleProcess`` exactly like
``worker/steps/process.py``: operation-signed uniform rates (or the signed
material table), deterministic ray-tracing parameters, the step advection
parameters, and the engine lease's execution profile. It never imports the
worker, :mod:`backend`, or a native library: ``ps`` always comes from the
engine lease.

``rateMode="material"`` keeps the native default rate at zero so materials
absent from ``materialRates`` do not move, which is the worker's material-mode
convention. A deposit duplicates the top level set with ``depositMaterial``
before the process runs, so the deposited material gets its own level set.

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
    material_handle,
    material_map,
    protected_material_names,
    signed_rates,
)
from process_models.v2.state import (
    LevelSetState,
    MaterialState,
    ProcessState,
    StateDelta,
    StepResult,
)

from ..params import SingleParticleParams

__all__ = ["SingleParticleNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "geometry", "solver")


class SingleParticleNativeVariant(EngineVariant):
    """Run one native ``ps.SingleParticleProcess`` step through shared helpers.

    Reads native geometry and materials, writes the mutated geometry plus the
    canonical material order. ``supports_masks=True`` because the uniform
    overload receives the project's geometry mask list. A deposit may introduce
    the deposited material, hence ``allow_new_materials=True``.
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
        state_profile=StateProfile(allow_new_materials=True),
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

    def validate(self, ctx: ProcessContext, params: SingleParticleParams) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Field ranges, finiteness, operation signs, the deposit material, and
        the rate-mode/table pairing are all enforced by
        :class:`~plugins.singleParticle.params.SingleParticleParams`, so this
        hook has nothing left to add.
        """
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: SingleParticleParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one single-particle step and return the committed state change.

        The native call sequence mirrors ``worker/steps/process.py``:
        operation-signed rate table, pinned flux engine, advection and ray
        parameters, then ``process.apply()``.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        operation = str(params.operation)
        rate_mode = str(params.rateMode)
        duration = float(params.duration)
        sticking = float(params.stickingProbability)
        source_exponent = float(params.sourceExponent)
        requested_rates = {name: float(rate) for name, rate in (params.materialRates or {}).items()}
        step = {
            "model": "singleParticle",
            "operation": operation,
            "rateMagnitude": float(params.rateMagnitude),
            "rateMode": rate_mode,
            "materialRates": requested_rates,
        }
        rate, signed_material_rates = signed_rates(step)

        mask_names: list[str] = []
        if rate_mode == "material":
            # The saved rateMagnitude is not a fallback in material mode: the
            # worker uses a zero native default rate so unlisted materials
            # (for example a mask) do not move.
            native_default_rate = 0.0
            model = ps.SingleParticleProcess(
                rate=native_default_rate,
                stickingProbability=sticking,
                sourceExponent=source_exponent,
                maskMaterials=[],
            )
            for name, signed_rate in signed_material_rates.items():
                model.setMaterialRate(material_handle(ps, name), signed_rate)
        else:
            native_default_rate = rate
            mask_names = list(protected_material_names(project, step))
            model = ps.SingleParticleProcess(
                rate=rate,
                stickingProbability=sticking,
                sourceExponent=source_exponent,
                maskMaterials=[material_handle(ps, name) for name in mask_names],
            )

        deposit_material: str | None = None
        if operation == "deposit":
            deposit_material = str(params.depositMaterial or "")
            if not deposit_material:
                raise RuntimeError("singleParticle deposit step requires depositMaterial")
            domain.duplicateTopLevelSet(material_handle(ps, deposit_material))

        process = ps.Process(domain, model, duration)
        execution = apply_execution_profile(ps, process, engine.profile)

        advection, advection_actual = configure_advection(ps, project)
        process.setParameters(advection)

        seed = ctx.seed
        if seed is None:
            random_settings = project.get("solver", {}).get("random") or {}
            if random_settings.get("mode") == "fixed" and random_settings.get("seed") is not None:
                seed = int(random_settings["seed"])
            else:
                raise RuntimeError(
                    "singleParticle requires ProcessContext.seed for a random-seed project; "
                    "the worker bridge passes the run seed"
                )
        rays, ray_actual = configure_rays(ps, project, int(seed))
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
            "nativeClass": "ps.SingleParticleProcess",
            "nativeConstructor": "keyword overload (rate, stickingProbability, sourceExponent, maskMaterials)",
            "operation": operation,
            "rateMode": rate_mode,
            "requestedRate": float(params.rateMagnitude),
            "signedRate": rate,
            "nativeDefaultRate": native_default_rate,
            "materialRates": requested_rates,
            "nativeMaterialRates": signed_material_rates,
            "stickingProbability": sticking,
            "sourceExponent": source_exponent,
            "depositMaterial": deposit_material,
            "maskMaterials": mask_names,
            "duration": duration,
            "timeStepRatio": float(advection.timeStepRatio),
            "timeStepRatioSource": advection_actual.get("timeStepRatioSource", "solver.timeStepRatio"),
            "rayTracing": ray_actual,
            "fluxEngineType": execution["kernel"],
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

    The shared advection/ray helpers read the schema version, geometry masks,
    and solver block. Silently substituting defaults would make the run record
    describe a project that was never used, so a partial context fails loudly.
    """
    project = ctx.project
    missing = [name for name in _REQUIRED_PROJECT_KEYS if not project.get(name)]
    if missing:
        raise RuntimeError(
            "singleParticle native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
