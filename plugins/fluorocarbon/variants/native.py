"""Native fluorocarbon plasma-etch variant.

The variant constructs the installed ``ps.FluorocarbonParameters`` object,
assigns the eight process scalars, the six ``Ions`` fields, and one
``ps.FluorocarbonMaterialParameters`` row per referenced material, then wraps
``ps.FluorocarbonEtching`` in a ``ps.Process`` whose flux engine comes from the
resolved execution profile. This is the same call sequence as
``worker/specialized.py::build_specialized_model`` plus the worker's advection
setup. It never imports the worker, :mod:`backend`, or a native library: ``ps``
always comes from the engine lease.

Referenced materials are the level-set materials in the input state, any
material named by an explicit table row, and the model's own ``Mask`` and
``Polymer`` pseudo-materials (the native surface model always expects both,
which is also why ``backend.validation`` requires a row for them). A
referenced material without a row is filled from
:data:`~plugins.fluorocarbon.params.FLUOROCARBON_MATERIAL_DEFAULTS`; that is
recorded in the step metadata so a run never silently claims native defaults
were explicit input.

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

from ..params import (
    FLUOROCARBON_MATERIAL_DEFAULTS,
    ION_FIELDS,
    MATERIAL_FIELDS,
    MODEL_FIELDS,
    POLYMER_MATERIAL,
    FluorocarbonParams,
)

__all__ = ["FluorocarbonNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")

#: The native surface model expects a parameter row for its mask material and
#: for the polymer it deposits even when neither is a level set.
_ALWAYS_REFERENCED_MATERIALS = frozenset({"Mask", POLYMER_MATERIAL})


class FluorocarbonNativeVariant(EngineVariant):
    """Run one native ``ps.FluorocarbonEtching`` step through the shared helpers.

    Reads native geometry and materials, writes the mutated geometry plus the
    canonical material order. ``supports_masks=True`` because the native plasma
    model assigns its own per-material rates; no geometry mask list is passed.
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

    def validate(self, ctx: ProcessContext, params: FluorocarbonParams) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Scalar ranges, finiteness, the complete material rows, and the known
        material-name set are already enforced by
        :class:`~plugins.fluorocarbon.params.FluorocarbonParams`, so this hook
        has nothing left to add.
        """
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: FluorocarbonParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one fluorocarbon step and return the committed state change.

        The native call sequence is the object-overload equivalent of
        ``worker/specialized.py::build_specialized_model`` followed by the
        worker's ``ps.Process`` setup: parameters, process, pinned flux engine,
        advection parameters, then ray tracing.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        scalars = params.scalar_values()
        parameters = ps.FluorocarbonParameters()
        for name in MODEL_FIELDS:
            setattr(parameters, name, scalars[name])
        ions = parameters.Ions
        for name in ION_FIELDS:
            setattr(ions, name, scalars[name])

        explicit_rows = params.material_values()
        referenced = _referenced_material_names(state, ps, explicit_rows)
        applied_rows: dict[str, dict[str, float]] = {}
        defaults_applied: list[str] = []
        for name in referenced:
            row_values = explicit_rows.get(name)
            if row_values is None:
                row_values = dict(FLUOROCARBON_MATERIAL_DEFAULTS)
                defaults_applied.append(name)
            row = ps.FluorocarbonMaterialParameters()
            row.id = ps.Material.Polymer if name == POLYMER_MATERIAL else material_handle(ps, name)
            for key in MATERIAL_FIELDS:
                setattr(row, key, float(row_values[key]))
            parameters.addMaterial(row)
            applied_rows[name] = dict(row_values)

        model = ps.FluorocarbonEtching(parameters)
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
                    "fluorocarbon requires ProcessContext.seed for a random-seed project; "
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
            "nativeClass": "ps.FluorocarbonEtching",
            "nativeConstructor": "FluorocarbonParameters object overload",
            "operation": "etch",
            "parameters": scalars,
            "materialParametersByMaterial": applied_rows,
            "materialDefaultsApplied": sorted(defaults_applied),
            "materials": list(referenced),
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


def _referenced_material_names(
    state: ProcessState,
    ps: Any,
    explicit_rows: dict[str, dict[str, float]],
) -> list[str]:
    """Return every material that needs a native parameter row.

    The level-set state is the authority for materials present in the domain.
    A state that carries only IDs (for example a hand-built test state) is
    resolved through the installed registry rather than silently skipping the
    material, which would leave the native defaults in place.
    """
    names = dict(state.materials.names)
    if state.materials.order:
        registry = ViennaPSMaterialRegistry(ps)
        for material_id in state.materials.order:
            if material_id not in names:
                resolved = registry.name_for(material_id)
                if resolved is None:
                    raise RuntimeError(f"fluorocarbon cannot resolve native material id {material_id} to a name")
                names[material_id] = resolved
    referenced = set(names.values()) | set(explicit_rows) | _ALWAYS_REFERENCED_MATERIALS
    return sorted(str(name) for name in referenced)


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
            "fluorocarbon native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
