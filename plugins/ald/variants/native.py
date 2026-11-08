"""Native single-particle ALD variant.

The variant duplicates the current top level set as the step's
``depositMaterial``, constructs ``ps.SingleParticleALDParams`` and
``ps.SingleParticleALD``, wraps them in a ``ps.Process`` whose flux engine comes
from the resolved execution profile, applies the atomic-layer parameters
(including ``numCycles``), and calls ``Process.apply()``. It never imports the
worker, :mod:`backend`, or a native library: ``ps`` always comes from the
engine lease.

Two native details are load-bearing:

The duplicate step creates the deposited material.
   ``SingleParticleALD`` has no output material of its own; it grows the top
   level set. The worker duplicates that set as ``depositMaterial`` before
   building the process, so the plugin does the same and names the new material
   from the request rather than guessing.

The model uses a stochastic ray flux.
   ``configure_rays`` pins ``useRandomSeeds=False`` and a concrete seed, which
   the caller supplies as ``ProcessContext.seed`` (the worker bridge passes the
   run seed) or the project's fixed solver seed. Without one the run would not
   be reproducible, so a random-seed project fails loudly.

``surfaceDiffusion`` is deliberately out of scope: steps that carry it are not
claimed by this process (see :mod:`plugins.ald.step`) because the 4.7.0 native
stencil can hang, so they keep the old worker path.

The variant mutates the caller's level-set handle in place
(``mutates_input=True``), exactly like the worker path. The executor is
responsible for checkpointing before calling it.
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

from ..params import ATOMIC_FIELDS, FLAT_FIELDS, NUM_CYCLES_FIELD, ALDParams

__all__ = ["ALDNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")


class ALDNativeVariant(EngineVariant):
    """Run one native ``ps.SingleParticleALD`` step through the shared helpers.

    Reads native geometry and materials, writes the duplicated deposit level
    set plus the canonical material order. ``supports_masks=False`` because the
    native model takes no mask list; it deposits on whatever surface it is
    given.

    ``GPU_DISK`` is advertised because it is backed by existing evidence for
    this model; there is no evidence for ``GPU_TRIANGLE``, so that profile is
    not offered.
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
            ExecutionProfile(backend="gpu", kernel="GPU_DISK"),
        ),
    )
    implementation_hash: str | None = None
    implementation_files = (("process_models/v2/engines/viennaps.py", getattr(_engine_helpers, "__file__", "")),)

    def validate(self, ctx: ProcessContext, params: ALDParams) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Field ranges, finiteness, probability bounds and the integer cycle
        count are already enforced by :class:`~plugins.ald.params.ALDParams`,
        so this hook has nothing left to add.
        """
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: ALDParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one ALD step and return the committed state change.

        The native call sequence matches the worker's deposit path for this
        model: duplicate the top level set as ``depositMaterial``, construct
        ``SingleParticleALDParams``/``SingleParticleALD``, build the process,
        pin the flux engine, set advection, the atomic-layer parameters, and
        the ray tracing, then apply.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        deposit_material = params.depositMaterial
        deposit_handle = material_handle(ps, deposit_material)
        deposit_id = int(deposit_handle)
        domain.duplicateTopLevelSet(deposit_handle)

        flat_values = params.flat_values()
        native_parameters = ps.SingleParticleALDParams()
        for name in FLAT_FIELDS:
            setattr(native_parameters, name, flat_values[name])
        model = ps.SingleParticleALD(native_parameters)

        process = ps.Process(domain, model, float(params.duration))
        execution = apply_execution_profile(ps, process, engine.profile)

        advection, advection_actual = configure_advection(ps, project)
        process.setParameters(advection)

        atomic_values = params.atomic_values()
        atomic = ps.AtomicLayerProcessParameters()
        for name in ATOMIC_FIELDS:
            setattr(atomic, name, atomic_values[name])
        atomic.numCycles = int(atomic_values[NUM_CYCLES_FIELD])
        process.setParameters(atomic)

        seed = ctx.seed
        if seed is None:
            random_settings = project.get("solver", {}).get("random") or {}
            if random_settings.get("mode") == "fixed":
                seed = int(random_settings.get("seed", 0))
            else:
                raise RuntimeError(
                    "ald requires ProcessContext.seed for a random-seed project; the worker bridge passes the run seed"
                )
        rays, ray_actual = configure_rays(ps, project, seed)
        process.setParameters(rays)

        process.apply()

        material_order = tuple(int(material_id) for material_id in material_map(domain))
        names = _material_names(ps, state.materials.names, material_order, deposit_id, deposit_material)
        delta_geometry = LevelSetState(
            handle=domain,
            dimension=geometry.dimension,
            grid_delta=geometry.grid_delta,
        )
        delta_materials = MaterialState(order=material_order, names=names)

        metadata: dict[str, Any] = {
            "nativeClass": "ps.SingleParticleALD",
            "nativeConstructor": "SingleParticleALD(SingleParticleALDParams)",
            "nativeParameterClass": "SingleParticleALDParams",
            "atomicLayerParameterClass": "AtomicLayerProcessParameters",
            "operation": "deposit",
            "depositMaterial": deposit_material,
            "duplicateTopLevelSet": True,
            "parameters": flat_values,
            "atomicLayerParameters": atomic_values,
            "numCycles": int(atomic_values[NUM_CYCLES_FIELD]),
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
    deposit_material: str,
) -> dict[int, str]:
    """Return names for every level set, including the freshly created deposit.

    The input state may only name its substrate (``{10: "Si"}``). The new
    level set exists only after ``duplicateTopLevelSet``, so its id is not in
    ``declared``; it is named from the step's ``depositMaterial``. Any other
    unnamed id is resolved through the installed native material registry
    instead of being left blank.
    """
    names = {int(material_id): name for material_id, name in declared.items()}
    registry = ViennaPSMaterialRegistry(ps)
    for material_id in material_order:
        material_id = int(material_id)
        if material_id not in names:
            native_name = registry.name_for(material_id)
            if native_name is not None:
                names[material_id] = native_name
    names[int(deposit_id)] = deposit_material
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
            "ald native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
