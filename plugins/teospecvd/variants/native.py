"""Native TEOS PECVD variant.

The variant constructs the installed ``ps.TEOSPECVD`` through its flat
eight-argument overload, wraps it in a ``ps.Process`` whose flux engine comes
from the resolved execution profile, and applies the same advection and ray
parameters the worker uses. The native model always deposits SiO2, so the
variant adds the new top level set before the process runs. It never imports
the worker, :mod:`backend`, or a native library: ``ps`` always comes from the
engine lease.

The variant mutates the caller's level-set handle in place
(``mutates_input=True``), exactly like the worker path. The executor is
responsible for checkpointing before calling it.

``nativeParameters.advanced`` is deliberately out of scope: steps that carry it
are not claimed by this process (see :mod:`plugins.teospecvd.step`) and keep
the old worker parameter-object path.
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
)
from process_models.v2.state import (
    LevelSetState,
    MaterialState,
    ProcessState,
    StateDelta,
    StepResult,
)

from ..params import FLAT_FIELDS, TEOSPECVDParams

__all__ = ["TEOSPECVDNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")
_DEPOSIT_MATERIAL = "SiO2"


class TEOSPECVDNativeVariant(EngineVariant):
    """Run one native ``ps.TEOSPECVD`` step through the shared helpers.

    Reads native geometry and materials, writes the mutated geometry plus the
    canonical material order. The step adds a new top level set, so the state
    profile allows new materials. ``supports_masks=False``: the native model has
    no mask argument and rates are implicit in the model parameters.
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
        state_profile=StateProfile(allow_new_materials=True),
    )
    engine = EngineRequirement(
        kind="native",
        engine_id="viennaps",
        min_version="4.6.2",
        profiles=(
            ExecutionProfile(backend="cpu", kernel="CPU_DISK"),
            ExecutionProfile(backend="cpu", kernel="CPU_TRIANGLE"),
            # Stage 9 evidence covers GPU_DISK only; this model has no verified
            # GPU_TRIANGLE path, so it is deliberately not advertised.
            ExecutionProfile(backend="gpu", kernel="GPU_DISK"),
        ),
    )
    implementation_hash: str | None = None
    implementation_files = (("process_models/v2/engines/viennaps.py", getattr(_engine_helpers, "__file__", "")),)

    def validate(self, ctx: ProcessContext, params: TEOSPECVDParams) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Field ranges and finiteness are already enforced by
        :class:`~plugins.teospecvd.params.TEOSPECVDParams`, so this hook has
        nothing left to add.
        """
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: TEOSPECVDParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one TEOS PECVD step and return the committed state change.

        The native call sequence is the flat-overload equivalent of
        ``worker/specialized.py::build_specialized_model`` followed by the
        worker's ``ps.Process`` setup: duplicate the SiO2 top level set, model,
        process, pinned flux engine, advection parameters, then rays.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        deposit_material = material_handle(ps, _DEPOSIT_MATERIAL)
        deposit_material_id = int(deposit_material)
        domain.duplicateTopLevelSet(deposit_material)

        flat_values = params.flat_values()
        model = ps.TEOSPECVD(*(flat_values[name] for name in FLAT_FIELDS))
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
                    "teospecvd requires ProcessContext.seed for a random-seed project; "
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
            names=_material_names(state, material_order, deposit_material_id),
        )

        metadata: dict[str, Any] = {
            "nativeClass": "ps.TEOSPECVD",
            "nativeConstructor": "flat eight-argument overload",
            "operation": "deposit",
            "parameters": flat_values,
            "duration": float(params.duration),
            "depositMaterial": _DEPOSIT_MATERIAL,
            "createdMaterialId": deposit_material_id,
            "addsNewLevelSetThrough": "Domain.duplicateTopLevelSet(SiO2) before Process.apply()",
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


def _material_names(
    state: ProcessState,
    material_order: tuple[int, ...],
    deposit_material_id: int,
) -> dict[int, str]:
    """Return canonical names for every material in ``material_order``.

    Existing names are preserved. The duplicated top level set is created by the
    native domain, so its id is absent from the incoming state when the input
    only had the substrate (for example Si); name it ``SiO2`` explicitly instead
    of leaving the material map unnamed.
    """
    names = {material_id: name for material_id, name in state.materials.names.items() if material_id in material_order}
    if deposit_material_id in material_order:
        names[deposit_material_id] = _DEPOSIT_MATERIAL
    return names


def _require_project(ctx: ProcessContext) -> Any:
    """Return ``ctx.project`` or raise when a required key is missing.

    The shared advection and ray helpers read the project schema version and
    the solver block. Silently substituting defaults would make the run record
    describe a project that was never used, so a partial context fails loudly.
    """
    project = ctx.project
    missing = [name for name in _REQUIRED_PROJECT_KEYS if not project.get(name)]
    if missing:
        raise RuntimeError(
            "teospecvd native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
