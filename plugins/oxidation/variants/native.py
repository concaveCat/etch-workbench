"""Native silicon thermal-oxidation variant.

The variant constructs ``ps.Oxidation`` through its no-argument constructor and
the same setter sequence as ``worker/specialized.py::build_specialized_model``:
oxidant (with the 4.7.0 ``DRY``/``WET`` enum fallback), orientation,
temperature, pressure, time, time step, transfer coefficient, initial oxide
thickness, activation volumes, and the silicon/oxide material handles. It then
wraps the model in a pinned ``CPU_DISK`` ``ps.Process`` whose duration is
``timeHours * 3600`` seconds, applies the shared advection parameters, and calls
``Process.apply()``.

No mask material and no rays are configured: this slice implements only the
unmasked path (masked steps are declined by
:mod:`plugins.oxidation.step`), and ``Oxidation`` is not a flux model.

The native model creates an oxide level set, so the step declares
``allow_new_materials=True`` and names the new material from the installed
native registry plus the step's ``oxideMaterial``. The variant mutates the
caller's level-set handle in place (``mutates_input=True``), exactly like the
worker path; the executor is responsible for checkpointing before calling it.

It never imports the worker, :mod:`backend`, or a native library: ``ps`` always
comes from the engine lease.
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

from ..params import OxidationParams

__all__ = ["OxidationNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")


class OxidationNativeVariant(EngineVariant):
    """Run one native ``ps.Oxidation`` step through the shared helpers.

    Reads native geometry and materials, writes the mutated geometry plus the
    canonical material order (the oxide level set is created by the model).
    ``supports_masks=False`` because the masked path is not claimed by this
    plugin. Only ``CPU_DISK`` is advertised: oxidation is not a flux model and
    has no verified GPU path on this host.
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
        profiles=(ExecutionProfile(backend="cpu", kernel="CPU_DISK"),),
    )
    implementation_hash: str | None = None
    implementation_files = (("process_models/v2/engines/viennaps.py", getattr(_engine_helpers, "__file__", "")),)

    def validate(self, ctx: ProcessContext, params: OxidationParams) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Field ranges, finiteness, positivity, and the oxidant/orientation
        enumerations are already enforced by
        :class:`~plugins.oxidation.params.OxidationParams`, so this hook has
        nothing left to add.
        """
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: OxidationParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one oxidation step and return the committed state change.

        The native call sequence matches ``worker/specialized.py`` for this
        model: construct ``Oxidation``, apply every setter, wrap it in a
        ``ps.Process`` with the derived duration, pin the resolved flux engine,
        apply advection parameters, then ``Process.apply()``.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        model = ps.Oxidation()
        oxidant = getattr(ps.OxidantType, params.oxidant, None)
        if oxidant is None:
            # viennals 5.8.5 spelled the members ``Dry``/``Wet``; 5.10.0
            # renamed them to ``DRY``/``WET``. Accept both.
            oxidant = getattr(ps.OxidantType, params.oxidant.upper(), None)
        if oxidant is None:
            raise RuntimeError(f"installed engine has no OxidantType {params.oxidant!r}")
        model.setOxidant(oxidant)
        model.setOrientation(getattr(ps.SiliconOrientation, params.orientation))
        model.setTemperature(float(params.temperatureC))
        model.setPressure(float(params.pressureAtm))
        model.setTime(float(params.timeHours))
        model.setTimeStep(float(params.timeStepHours))
        model.setTransferCoefficient(float(params.transferCoefficientUmHr))
        model.setInitialOxideThickness(float(params.initialOxideThicknessUm))
        model.setReactionActivationVolume(float(params.reactionActivationVolumeM3))
        model.setDiffusionActivationVolume(float(params.diffusionActivationVolumeM3))
        model.setSiliconMaterial(material_handle(ps, params.siliconMaterial))
        model.setOxideMaterial(material_handle(ps, params.oxideMaterial))

        duration = params.duration_seconds
        process = ps.Process(domain, model, duration)
        execution = apply_execution_profile(ps, process, engine.profile)

        advection, advection_actual = configure_advection(ps, project)
        process.setParameters(advection)

        process.apply()

        material_order = tuple(int(material_id) for material_id in material_map(domain))
        names = _material_names(ps, state.materials.names, material_order, params)
        delta_geometry = LevelSetState(
            handle=domain,
            dimension=geometry.dimension,
            grid_delta=geometry.grid_delta,
        )
        delta_materials = MaterialState(order=material_order, names=names)

        metadata: dict[str, Any] = {
            "nativeClass": "ps.Oxidation",
            "nativeConstructor": "no-argument constructor + setters",
            "operation": "native",
            "parameters": params.native_values(),
            "durationSeconds": duration,
            "durationDerivation": "nativeParameters.timeHours * 3600.0",
            "oxidant": params.oxidant,
            "oxidantNative": getattr(oxidant, "name", str(oxidant)),
            "orientation": params.orientation,
            "siliconMaterial": params.siliconMaterial,
            "oxideMaterial": params.oxideMaterial,
            "timeStepRatio": float(advection.timeStepRatio),
            "timeStepRatioSource": advection_actual.get("timeStepRatioSource", "solver.timeStepRatio"),
            "fluxEngine": execution["kernel"],
            "executionProfile": execution,
            "rayTracing": None,
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
    params: OxidationParams,
) -> dict[int, str]:
    """Return names for every level set, including the created oxide.

    The input state may only name its substrate (``{10: "Si"}``). The oxide
    level set exists only after ``Process.apply()``, so its id is not in
    ``declared``; it is named from the step's ``oxideMaterial``/``siliconMaterial``.
    Any other unnamed id is resolved through the installed native material
    registry instead of being left blank.
    """
    names = {int(material_id): name for material_id, name in declared.items()}
    registry = ViennaPSMaterialRegistry(ps)
    for material_id in material_order:
        material_id = int(material_id)
        if material_id not in names:
            native_name = registry.name_for(material_id)
            if native_name is not None:
                names[material_id] = native_name
    oxide_id = int(material_handle(ps, params.oxideMaterial))
    silicon_id = int(material_handle(ps, params.siliconMaterial))
    names[oxide_id] = params.oxideMaterial
    if silicon_id != oxide_id:
        names[silicon_id] = params.siliconMaterial
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
            "oxidation native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
