"""Native isotropic etch/deposit variant.

The variant builds the same synthetic step dictionary the worker would see,
calls the same rate/advection helpers from
:mod:`process_models.v2.engines.viennaps`, and constructs the same
``ps.IsotropicProcess``. It never imports the worker.

The variant mutates the caller's level-set handle in place
(``mutates_input=True``), exactly like the worker path. The executor is
responsible for checkpointing before calling it. The engine lease is passed
explicitly to :meth:`IsotropicNativeVariant.execute`; it is never stored in the
process state.

The variant requires a complete project (``schemaVersion``, ``geometry``, and
``solver``): the shared helpers read all three, so a partially filled context
fails loudly instead of falling back to defaults that would not describe the
run. :class:`~process_models.v2.state.MaterialState` is the single canonical
material order; the variant returns it as a delta next to the geometry.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

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

__all__ = ["IsotropicNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "geometry", "solver")


class IsotropicNativeVariant(EngineVariant):
    """Run a native ``ps.IsotropicProcess`` step through the shared engine helpers.

    The variant reads native geometry and writes both the mutated geometry and
    the canonical material order. It declares ``mutates_input=True`` because the
    process advects the caller's domain in place.
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
        profiles=(ExecutionProfile(backend="cpu", kernel="CPU_DISK"),),
    )
    implementation_hash: str | None = None
    implementation_files = (("process_models/v2/engines/viennaps.py", getattr(_engine_helpers, "__file__", "")),)

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        """Return the semantic issues Pydantic and the resolver cannot express.

        Parameter ranges (dimension, positive finite rates/duration, finite
        non-negative material rates) are already enforced by the parameter model
        and the resolver, so only mode-specific rules and the deposit material
        are checked here.
        """
        issues: list[Issue] = []

        rate_mode = str(getattr(params, "rateMode", "uniform"))
        operation = str(getattr(params, "operation", "etch"))
        material_rates = dict(getattr(params, "materialRates", {}) or {})
        mask_materials = list(getattr(params, "maskMaterials", []) or [])

        if rate_mode == "material":
            if not material_rates:
                issues.append(
                    Issue(
                        path="materialRates",
                        message="rateMode 'material' requires a non-empty material rate table",
                    )
                )
            if mask_materials:
                # The worker uses no geometry masks in material mode: the table
                # alone decides which materials move.
                issues.append(
                    Issue(
                        path="maskMaterials",
                        message=(
                            "rateMode 'material' ignores maskMaterials; "
                            "leave unlisted materials out of materialRates instead"
                        ),
                        severity="warning",
                    )
                )
        elif material_rates:
            issues.append(
                Issue(
                    path="materialRates",
                    message="rateMode 'uniform' forbids materialRates",
                )
            )

        if operation == "deposit":
            deposit_material = str(getattr(params, "depositMaterial", "") or "")
            if not deposit_material.strip():
                issues.append(
                    Issue(
                        path="depositMaterial",
                        message="deposit steps require a non-empty depositMaterial",
                    )
                )

        return issues

    def execute(
        self,
        state: ProcessState,
        params: BaseModel,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one isotropic step and return the committed state change.

        ``result.delta`` carries the mutated native geometry and the canonical
        material order; ``metadata`` and ``warnings`` are report data and never
        count as a state change. The helpers come from
        :mod:`process_models.v2.engines.viennaps`, so the plugin never imports
        the worker or native libraries. ``engine.handle`` is the installed
        ``viennaps`` module supplied by the executor's lease.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        operation = str(getattr(params, "operation", "etch"))
        rate_mode = str(getattr(params, "rateMode", "uniform"))
        material_rates = dict(getattr(params, "materialRates", {}) or {})
        duration = float(getattr(params, "duration", 0.0))
        deposit_material = str(getattr(params, "depositMaterial", "SiO2"))
        time_step_ratio = getattr(params, "isotropicTimeStepRatio", None)

        step = {
            "model": "isotropic",
            "operation": operation,
            "rateMagnitude": float(getattr(params, "rateMagnitude", 0.0)),
            "duration": duration,
            "depositMaterial": deposit_material,
            "rateMode": rate_mode,
            "materialRates": material_rates,
            "isotropicTimeStepRatio": time_step_ratio,
        }

        if operation == "deposit":
            domain.duplicateTopLevelSet(material_handle(ps, deposit_material))

        rate, signed_material_rates = signed_rates(step)
        warnings: list[str] = []

        if rate_mode == "material":
            # The rate table is authoritative. The worker's validated material
            # mode uses rateMagnitude=0, so unlisted materials (for example a
            # mask) must not inherit the uniform fallback rate.
            native_default_rate = 0.0
            mask_names: list[str] = []
            if getattr(params, "maskMaterials", None):
                warnings.append("rateMode 'material' ignores maskMaterials; the material rate table is authoritative")
            model = ps.IsotropicProcess(rate=native_default_rate, maskMaterials=[])
            for name, signed_rate in signed_material_rates.items():
                model.setMaterialRate(material_handle(ps, name), signed_rate)
        else:
            native_default_rate = rate
            explicit_masks = list(getattr(params, "maskMaterials", None) or [])
            mask_names = explicit_masks or protected_material_names(project, step)
            model = ps.IsotropicProcess(
                rate=rate,
                maskMaterials=[material_handle(ps, name) for name in mask_names],
            )

        process = ps.Process(domain, model, duration)
        execution = apply_execution_profile(ps, process, engine.profile)
        advection, advection_actual = configure_advection(
            ps,
            project,
            time_step_ratio=time_step_ratio,
            override=getattr(params, "advection", None),
        )
        process.setParameters(advection)
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
            "operation": operation,
            "rateMode": rate_mode,
            "requestedRate": float(getattr(params, "rateMagnitude", 0.0)),
            "actualRate": abs(rate),
            "signedRate": rate,
            "nativeDefaultRate": native_default_rate,
            "duration": duration,
            "depositMaterial": deposit_material if operation == "deposit" else None,
            "materialRates": material_rates,
            "nativeMaterialRates": signed_material_rates,
            "maskMaterials": mask_names,
            "timeStepRatio": float(advection.timeStepRatio),
            "timeStepRatioSource": advection_actual.get(
                "timeStepRatioSource",
                "solver.timeStepRatio",
            ),
            "advectionOverride": advection_actual.get("overrideFields", []),
            "nativeClass": "ps.IsotropicProcess",
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
            warnings=warnings,
        )


def _require_project(ctx: ProcessContext) -> Any:
    """Return ``ctx.project`` or raise when a required key is missing.

    The shared helpers always read the project schema version, the geometry
    masks, and the solver time-step ratio. Callers (tests included) must pass a
    complete project; silently substituting defaults would make the run record
    describe a project that was never used.
    """
    project = ctx.project
    missing = [name for name in _REQUIRED_PROJECT_KEYS if not project.get(name)]
    if missing:
        raise RuntimeError(
            "isotropic native variant requires a complete project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
