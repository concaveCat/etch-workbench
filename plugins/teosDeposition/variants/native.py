"""Native TEOS thermal-deposition variant.

The variant duplicates the current top level set as SiO2, constructs the
installed ``ps.TEOSDeposition`` through its six-argument constructor, wraps it
in a ``ps.Process`` whose flux engine comes from the resolved execution
profile, and applies the same advection and ray-tracing parameters the worker
uses. It never imports the worker, :mod:`backend`, or a native library: ``ps``
always comes from the engine lease.

The duplicate step is required by the native model and by the stage-8 direct
reference: ``TEOSDeposition`` grows the new SiO2 level set, it does not convert
the substrate in place. The variant mutates the caller's level-set handle in
place (``mutates_input=True``), exactly like the worker path. The executor is
responsible for checkpointing before calling it.

``nativeParameters.advanced`` is deliberately out of scope: steps that carry it
are not claimed by this process (see :mod:`plugins.teosDeposition.step`) and
keep the old worker parameter-object path.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

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
    ViennaPSMaterialRegistry,
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

from ..params import FLAT_FIELDS, TEOSDepositionParams

__all__ = ["TEOSDepositionNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")
_DEPOSIT_MATERIAL = "SiO2"


class TEOSDepositionNativeVariant(EngineVariant):
    """Run one native ``ps.TEOSDeposition`` step through the shared helpers.

    The flat TEOSDeposition constructor has no GPU model conversion in the
    installed 4.7.0 runtime (the native layer warns and keeps the CPU model), so
    this plugin deliberately does not advertise GPU_DISK.

    Reads native geometry and materials, writes the duplicated SiO2 level set
    plus the canonical material order. ``supports_masks=False`` because the
    native model has no mask list; it deposits on whatever surface it is given.
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
        ),
    )
    implementation_hash: str | None = None
    implementation_files = (("process_models/v2/engines/viennaps.py", getattr(_engine_helpers, "__file__", "")),)

    def validate(self, ctx: ProcessContext, params: TEOSDepositionParams) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Field ranges and finiteness are already enforced by
        :class:`~plugins.teosDeposition.params.TEOSDepositionParams`, so this
        hook has nothing left to add.
        """
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: TEOSDepositionParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one TEOS deposition step and return the committed state change.

        The native call sequence matches the worker's deposit path: duplicate
        the top level set as the model's output material, then construct the
        model/process, pin the flux engine, and set advection plus ray tracing.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        deposit_handle = material_handle(ps, _DEPOSIT_MATERIAL)
        deposit_id = int(deposit_handle)
        domain.duplicateTopLevelSet(deposit_handle)

        flat_values = params.flat_values()
        model = ps.TEOSDeposition(*(flat_values[name] for name in FLAT_FIELDS))
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
                    "teosDeposition requires ProcessContext.seed for a random-seed project; "
                    "the worker bridge passes the run seed"
                )
        rays, ray_actual = configure_rays(ps, project, seed)
        process.setParameters(rays)

        process.apply()

        material_order = tuple(int(material_id) for material_id in material_map(domain))
        names = _material_names(ps, state.materials.names, material_order, deposit_id)
        delta_geometry = LevelSetState(
            handle=domain,
            dimension=geometry.dimension,
            grid_delta=geometry.grid_delta,
        )
        delta_materials = MaterialState(order=material_order, names=names)

        metadata: dict[str, Any] = {
            "nativeClass": "ps.TEOSDeposition",
            "nativeConstructor": "six-argument constructor",
            "operation": "deposit",
            "depositMaterial": _DEPOSIT_MATERIAL,
            "duplicateTopLevelSet": True,
            "parameters": flat_values,
            "duration": float(params.duration),
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
    ps: Any,
    declared: Mapping[int, str],
    material_order: Iterable[int],
    deposit_id: int,
) -> dict[int, str]:
    """Return names for every level set, including the freshly created SiO2.

    The input state may only name its substrate (``{10: "Si"}``). The new
    level set exists only after ``duplicateTopLevelSet``, so its id is not in
    ``declared``; it is named explicitly from the plugin's fixed output
    material. Any other unnamed id is resolved through the installed native
    material registry instead of being left blank.
    """
    names = {int(material_id): name for material_id, name in declared.items()}
    registry = ViennaPSMaterialRegistry(ps)
    for material_id in material_order:
        material_id = int(material_id)
        if material_id not in names:
            native_name = registry.name_for(material_id)
            if native_name is not None:
                names[material_id] = native_name
    names[int(deposit_id)] = _DEPOSIT_MATERIAL
    order_set = {int(material_id) for material_id in material_order}
    return {material_id: name for material_id, name in names.items() if material_id in order_set}


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
            "teosDeposition native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
