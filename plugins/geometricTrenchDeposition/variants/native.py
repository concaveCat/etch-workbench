"""Native GeometricTrenchDeposition variant.

The variant duplicates the current top level set as the step's
``depositMaterial``, constructs the installed
``ps.GeometricTrenchDeposition`` through its seven-argument constructor, wraps
it in a ``ps.Process`` whose flux engine comes from the resolved execution
profile, applies the shared advection parameters, and calls
``Process.apply()``. It never imports the worker, :mod:`backend`, or a native
library: ``ps`` always comes from the engine lease.

Two native semantics are load-bearing and are recorded in the step metadata:

``GeometricTrenchDeposition`` is a ViennaLS geometric-advection distribution,
not a velocity-field model.
   It has no material table, no native mask, and one geometric advection per
   ``Process.apply()``. The deposited thickness is ``a*(1-|z|/trenchDepth)^n
   + b`` (or ``bottomMed`` at the floor) with the trench top at ``z = 0``; the
   process duration is ignored. The duplicate step is required because the
   kernel grows that new level set instead of converting the substrate.

The reach guard is mandatory.
   A ``depositionRate`` below ``max(a + b, bottomMed)`` leaves the deposited
   level set empty and can SIGSEGV inside the kernel. The variant re-checks it
   before ``duplicateTopLevelSet``/``ps.Process`` so a caller that bypasses
   :func:`plugins.geometricTrenchDeposition.step.parse_step` still cannot reach
   the native call.

No flux or rays are configured: the model has no flux. ``ps.Process`` is still
pinned to ``CPU_DISK`` through the resolved profile, and only that profile is
advertised.

The variant mutates the caller's level-set handle in place
(``mutates_input=True``), exactly like the worker path. The executor is
responsible for checkpointing before calling it.

``nativeParameters.advanced`` is deliberately out of scope: steps that carry it
are not claimed by this process (see
:mod:`plugins.geometricTrenchDeposition.step`) and keep the old worker
parameter-object path.
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

from ..params import FLAT_FIELDS, GeometricTrenchDepositionParams

__all__ = ["GeometricTrenchDepositionNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")

#: Absolute-z reference the installed kernel uses for its local radius.
_DEPTH_REFERENCE = (
    "absolute z; radius = a*(1-|z|/trenchDepth)^n + b with the trench top assumed at z=0 "
    "and bottomMed at z=-trenchDepth"
)


class GeometricTrenchDepositionNativeVariant(EngineVariant):
    """Run one native ``ps.GeometricTrenchDeposition`` step.

    The model has no flux engine and no verified GPU conversion in the
    installed runtime, so only ``CPU_DISK`` on the CPU backend is advertised.
    Reads native geometry and materials, writes the duplicated deposit level
    set plus the canonical material order. ``supports_masks=False`` because the
    native model takes no mask list.
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

    def validate(self, ctx: ProcessContext, params: GeometricTrenchDepositionParams) -> list[Issue]:
        """Return the semantic issues Pydantic and the resolver cannot express.

        Field ranges and finiteness are already enforced by
        :class:`~plugins.geometricTrenchDeposition.params.GeometricTrenchDepositionParams`.
        What remains is the cross-field reach guard and the GDS refusal (a GDS
        project is also declined by ``parse_step``; this keeps a hand-built
        request from reaching the native constructor).
        """
        issues: list[Issue] = []
        if violation := params.reach_guard_violation():
            issues.append(Issue(path="depositionRate", message=violation))
        geometry = ctx.project.get("geometry")
        if isinstance(geometry, Mapping) and geometry.get("kind") == "gds":
            issues.append(
                Issue(
                    path="geometry",
                    message=(
                        "GeometricTrenchDeposition is not supported with GDS geometry; its native radius "
                        "formula is evaluated from absolute z and a GDS level set has no validated vertical span"
                    ),
                )
            )
        return issues

    def execute(
        self,
        state: ProcessState,
        params: GeometricTrenchDepositionParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one geometric-trench deposition and return the state change.

        The native call sequence matches the worker's deposit path for this
        model: duplicate the top level set as ``depositMaterial``, then
        construct model/process, pin the flux engine, set advection, and apply.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        if violation := params.reach_guard_violation():
            # Reject before duplicateTopLevelSet and ps.Process so a caller
            # that bypassed parse_step cannot reach the native kernel.
            raise RuntimeError(f"geometricTrenchDeposition rejected before the native call: {violation}")

        deposit_material = params.depositMaterial
        deposit_handle = material_handle(ps, deposit_material)
        deposit_id = int(deposit_handle)
        domain.duplicateTopLevelSet(deposit_handle)

        arguments = params.flat_values()
        model = ps.GeometricTrenchDeposition(*(arguments[name] for name in FLAT_FIELDS))
        process = ps.Process(domain, model, float(params.duration))
        execution = apply_execution_profile(ps, process, engine.profile)

        advection, advection_actual = configure_advection(ps, project)
        process.setParameters(advection)

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
            "nativeClass": "ps.GeometricTrenchDeposition",
            "nativeConstructor": "seven-argument constructor",
            "operation": "deposit",
            "depositMaterial": deposit_material,
            "duplicateTopLevelSet": True,
            "arguments": dict(arguments),
            "parameters": dict(arguments),
            "duration": float(params.duration),
            "durationIgnored": True,
            "durationScalesThickness": False,
            "reachGuard": dict(params.reach_guard()),
            "trenchWidthUsedByNativeKernel": False,
            "depthReference": _DEPTH_REFERENCE,
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
    deposit_id: int,
    deposit_material: str,
) -> dict[int, str]:
    """Return names for every level set, including the freshly created deposit.

    The input state may only name its substrate (``{10: "Si"}``). The new
    level set exists only after ``duplicateTopLevelSet``, so its id is not in
    ``declared``; it is named from the step's ``depositMaterial``. Any other
    unnamed id (for example the trench mask material) is resolved through the
    installed native material registry instead of being left blank.
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
            "geometricTrenchDeposition native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
