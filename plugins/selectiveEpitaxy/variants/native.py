"""Native selective SiGe epitaxy variant.

The variant constructs the installed ``ps.SelectiveEpitaxy`` through the same
two overloads as ``worker/specialized.py::build_specialized_model``:

* ``ps.SelectiveEpitaxy([(material, rate), ...], rate111, rate100)`` where every
  material is the installed native enum itself, or
* ``ps.SelectiveEpitaxy([f0, f1, f2], rate111, rate100)`` followed by one
  ``setMaterialRate(material, rate)`` per material, because that overload
  leaves the model's rate table empty while its velocity field holds that map
  by reference.

The native epitaxy constructor checks the enum identity of its top level set,
so the rows and ``duplicateTopLevelSet`` use ``getattr(ps.Material, name)``
instead of a registry handle: a registry handle does not satisfy that check.

The model grows SiGe, so the current top level set is duplicated as
``ps.Material.SiGe`` before ``ps.Process`` is built. The variant mutates the
caller's level-set handle in place (``mutates_input=True``), exactly like the
worker path; the executor is responsible for checkpointing before calling it.

There is no flux and no ray tracing, but the process still pins the flux
engine to ``CPU_DISK``: without it the native process may initialize the CUDA
code path even though this model never uses it.

The variant never imports the worker, :mod:`backend`, or a native library;
``ps`` always comes from the engine lease.
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
    material_map,
)
from process_models.v2.state import (
    LevelSetState,
    MaterialState,
    ProcessState,
    StateDelta,
    StepResult,
)

from ..params import SelectiveEpitaxyParams

__all__ = ["SelectiveEpitaxyNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")
_DEPOSIT_MATERIAL = "SiGe"


class SelectiveEpitaxyNativeVariant(EngineVariant):
    """Run one native ``ps.SelectiveEpitaxy`` step through the shared helpers.

    Reads native geometry and materials, writes the duplicated SiGe level set
    plus the canonical material order. ``supports_masks=False`` because the
    native model has no mask list; it deposits on whatever surface it is given.
    The flat ``SelectiveEpitaxy`` overload has no GPU model conversion, so this
    plugin deliberately advertises CPU_DISK only.
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

    def validate(self, ctx: ProcessContext, params: SelectiveEpitaxyParams) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Field ranges, finiteness, the non-empty material table, and the
        three-component ``nvFactors`` vector are already enforced by
        :class:`~plugins.selectiveEpitaxy.params.SelectiveEpitaxyParams`, so
        this hook has nothing left to add.
        """
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: SelectiveEpitaxyParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one selective-epitaxy step and return the committed state change.

        The native call sequence matches the worker's deposit path: duplicate
        the top level set as the model's output material, build the model
        through the requested overload, pin the flux engine, and set the
        advection parameters. Ray tracing is deliberately absent because
        ``SelectiveEpitaxy`` does not consume a flux field.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        # The native epitaxy constructor checks the enum identity of its top
        # level set, so this must be the installed enum and not a handle.
        deposit_material = getattr(ps.Material, _DEPOSIT_MATERIAL)
        deposit_id = int(deposit_material)
        domain.duplicateTopLevelSet(deposit_material)

        rates = [(getattr(ps.Material, name), float(rate)) for name, rate in params.sorted_material_rates()]
        if params.nvFactors is None:
            model = ps.SelectiveEpitaxy(rates, float(params.rate111), float(params.rate100))
            overload = "materialRates"
            constructor = "viennaps.d%d.SelectiveEpitaxy(materialRates, rate111, rate100)"
        else:
            factors = [float(value) for value in params.nvFactors]
            model = ps.SelectiveEpitaxy(factors, float(params.rate111), float(params.rate100))
            for material, rate in rates:
                model.setMaterialRate(material, rate)
            overload = "nvFactors"
            constructor = "viennaps.d%d.SelectiveEpitaxy(nvFactors, rate111, rate100) + setMaterialRate"

        process = ps.Process(domain, model, float(params.duration))
        execution = apply_execution_profile(ps, process, engine.profile)

        advection, advection_actual = configure_advection(ps, project, override=getattr(params, "advection", None))
        process.setParameters(advection)

        process.apply()

        material_order = tuple(int(material_id) for material_id in material_map(domain))
        names = _material_names(ps, state.materials.names, material_order, deposit_id)
        delta_geometry = LevelSetState(
            handle=domain,
            dimension=geometry.dimension,
            grid_delta=geometry.grid_delta,
        )
        delta_materials = MaterialState(order=material_order, names=names)

        rates_json = {name: float(rate) for name, rate in params.sorted_material_rates()}
        metadata: dict[str, Any] = {
            "nativeClass": "ps.SelectiveEpitaxy",
            "nativeConstructor": constructor % int(geometry.dimension),
            "overload": overload,
            "operation": "deposit",
            "depositMaterial": _DEPOSIT_MATERIAL,
            "duplicateTopLevelSet": True,
            "nvFactors": None if params.nvFactors is None else [float(value) for value in params.nvFactors],
            "rates": rates_json,
            "rate111": float(params.rate111),
            "rate100": float(params.rate100),
            "parameters": params.native_parameters(),
            "duration": float(params.duration),
            "timeStepRatio": float(advection.timeStepRatio),
            "timeStepRatioSource": advection_actual.get("timeStepRatioSource", "solver.timeStepRatio"),
            "advectionOverride": advection_actual.get("overrideFields", []),
            "maxDissipation": advection_actual.get("maxDissipation"),
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


def _material_names(
    ps: Any,
    declared: Mapping[int, str],
    material_order: Iterable[int],
    deposit_id: int,
) -> dict[int, str]:
    """Return names for every level set, including the freshly created SiGe.

    The input state may only name its substrate (``{10: "Si"}``). The new level
    set exists only after ``duplicateTopLevelSet``, so its id is not in
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
            "selectiveEpitaxy native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
