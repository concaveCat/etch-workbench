"""Native ``ps.DirectionalProcess`` variant.

The variant reproduces the three branches of ``worker/steps/process.py``:

* scalar: ``DirectionalProcess(direction, directionalRate, isotropicRate,
  maskMaterials, calculateVisibility)``;
* rateSets: ``DirectionalProcess(Sequence[RateSet])``;
* materialRates: ``DirectionalProcess(direction, rateMap, defaultDirectional,
  defaultIsotropic)``.

A deposit duplicates the top level set with ``depositMaterial`` before the
process runs, exactly like the worker. Directional models have no stochastic
flux engine, so the only declared execution profile is ``CPU_DISK`` and no ray
tracing parameters are configured.

The variant mutates the caller's level-set handle in place
(``mutates_input=True``), exactly like the worker path. The executor is
responsible for checkpointing before calling it. ``ps`` always comes from the
engine lease: this module never imports the worker, :mod:`backend`, or a native
library.
"""

from __future__ import annotations

from typing import Any, Mapping

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
)
from process_models.v2.state import (
    LevelSetState,
    MaterialState,
    ProcessState,
    StateDelta,
    StepResult,
)

from ..params import DirectionalParams

__all__ = ["DirectionalNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")

SCALAR_OVERLOAD = "DirectionalProcess(direction, directionalRate, isotropicRate, maskMaterials, calculateVisibility)"
RATE_SET_OVERLOAD = "DirectionalProcess(rateSets: Sequence[RateSet])"
MATERIAL_RATE_OVERLOAD = (
    "DirectionalProcess(direction, materialRates: Mapping[Material, (directional, isotropic)], "
    "defaultDirectionalRate, defaultIsotropicRate)"
)
_DIRECTION_COORDINATE_MAP = "2D [x,h,0] -> native [x,h,0]; 3D [x,y,z] -> native [x,y,z] (z vertical)"
_NATIVE_PROCESS_BACKEND = "CPU_DISK pinned; directional model has no stochastic flux engine"


def _material_id(handle: Any) -> int:
    return int(handle)


def _signed_scalar_rates(params: DirectionalParams) -> tuple[float, float]:
    sign = -1.0 if params.operation == "etch" else 1.0
    if params.directionalRateMagnitude is None or params.isotropicRateMagnitude is None:
        raise RuntimeError("scalar directional step is missing its rate magnitudes")
    return float(params.directionalRateMagnitude) * sign, float(params.isotropicRateMagnitude) * sign


def _rate_set_visibility(rate_set: Any) -> bool:
    """Per-set visibility: requested and meaningful only with a directional rate."""
    return bool(rate_set.calculateVisibility) and float(rate_set.directionalVelocity) > 0.0


class DirectionalNativeVariant(EngineVariant):
    """Run one native ``ps.DirectionalProcess`` step through the shared helpers."""

    variant_id = "native"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry", "materials"}),
        writes=frozenset({"geometry", "materials"}),
        supports_masks=True,
        mutates_input=True,
        determinism="strict",
        # A deposit calls duplicateTopLevelSet(depositMaterial), which can
        # introduce a material that was not in the input state.
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

    def validate(self, ctx: ProcessContext, params: DirectionalParams) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Mode mutual exclusion, rate ranges, nonzero directions and the deposit
        material rule are all enforced by :class:`~plugins.directional.params.DirectionalParams`.
        """
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: DirectionalParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one directional step and return the committed state change."""
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle
        step = ctx.step or None

        if params.operation == "deposit":
            deposit_material = params.depositMaterial
            if deposit_material is None:
                raise RuntimeError("directional deposit requires depositMaterial")
            domain.duplicateTopLevelSet(material_handle(ps, deposit_material))

        mode = params.mode()
        metadata: dict[str, Any] = {
            "nativeClass": "ps.DirectionalProcess",
            "mode": mode,
            "operation": params.operation,
            "duration": float(params.duration),
            "directionCoordinateMap": _DIRECTION_COORDINATE_MAP,
            "nativeProcessBackend": _NATIVE_PROCESS_BACKEND,
            "raysPerPoint": "not-applicable",
            "randomSeed": "not-applicable",
        }
        if params.operation == "deposit":
            metadata["depositMaterial"] = params.depositMaterial

        if mode == "rateSets":
            model = self._build_rate_sets(ps, params, project, step, metadata)
        elif mode == "materialRates":
            model = self._build_material_rates(ps, params, metadata)
        else:
            model = self._build_scalar(ps, params, project, step, metadata)

        process = ps.Process(domain, model, float(params.duration))
        execution = apply_execution_profile(ps, process, engine.profile)
        advection, advection_actual = configure_advection(ps, project, override=getattr(params, "advection", None))
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

        metadata.update(
            {
                "timeStepRatio": float(advection.timeStepRatio),
                "timeStepRatioSource": advection_actual.get("timeStepRatioSource", "solver.timeStepRatio"),
                "advectionOverride": advection_actual.get("overrideFields", []),
                "fluxEngine": execution["kernel"],
                "executionProfile": execution,
                "dimension": geometry.dimension,
                "gridDelta": geometry.grid_delta,
                "levelSetCount": int(domain.getNumberOfLevelSets()),
                "materialMap": list(material_order),
            }
        )

        return StepResult(
            delta=StateDelta(geometry=delta_geometry, materials=delta_materials),
            metadata=metadata,
            warnings=[],
        )

    def _build_scalar(
        self,
        ps: Any,
        params: DirectionalParams,
        project: Mapping[str, Any],
        step: Mapping[str, Any] | None,
        metadata: dict[str, Any],
    ) -> Any:
        requested_direction = list(params.direction or [])
        unit_direction = params.unit_direction()
        directional_velocity, isotropic_velocity = _signed_scalar_rates(params)
        mask_names = protected_material_names(dict(project), None if step is None else dict(step))
        mask_materials = [material_handle(ps, name) for name in mask_names]
        visibility = bool(params.calculateVisibility) if float(params.directionalRateMagnitude or 0.0) > 0.0 else False
        model = ps.DirectionalProcess(
            unit_direction,
            directional_velocity,
            isotropic_velocity,
            mask_materials,
            visibility,
        )
        metadata.update(
            {
                "overload": SCALAR_OVERLOAD,
                "nativeOverload": SCALAR_OVERLOAD,
                "requestedDirection": requested_direction,
                "nativeUnitDirection": unit_direction,
                "directionalRateMagnitude": float(params.directionalRateMagnitude or 0.0),
                "isotropicRateMagnitude": float(params.isotropicRateMagnitude or 0.0),
                "nativeDirectionalVelocity": directional_velocity,
                "nativeIsotropicVelocity": isotropic_velocity,
                "calculateVisibility": (
                    visibility
                    if float(params.directionalRateMagnitude or 0.0) > 0.0
                    else "not-applicable: directional rate is zero"
                ),
                "visibility": visibility,
                "masks": mask_names,
                "maskMaterials": mask_names,
                "maskMaterialIds": [int(material) for material in mask_materials],
            }
        )
        return model

    def _build_rate_sets(
        self,
        ps: Any,
        params: DirectionalParams,
        project: Mapping[str, Any],
        step: Mapping[str, Any] | None,
        metadata: dict[str, Any],
    ) -> Any:
        if params.rateSets is None:
            raise RuntimeError("rate-set directional step is missing rateSets")
        sign = -1.0 if params.operation == "etch" else 1.0
        default_masks = protected_material_names(dict(project), None if step is None else dict(step))
        native_sets: list[Any] = []
        records: list[dict[str, Any]] = []
        for index, rate_set in enumerate(params.rateSets):
            length = sum(component * component for component in rate_set.direction) ** 0.5
            unit_direction = [component / length for component in rate_set.direction]
            requested_masks = rate_set.maskMaterials
            mask_names = list(default_masks) if requested_masks is None else list(requested_masks)
            mask_materials = [material_handle(ps, name) for name in mask_names]
            directional_velocity = float(rate_set.directionalVelocity) * sign
            isotropic_velocity = float(rate_set.isotropicVelocity) * sign
            visibility = _rate_set_visibility(rate_set)
            native_sets.append(
                ps.RateSet(
                    unit_direction,
                    directional_velocity,
                    isotropic_velocity,
                    mask_materials,
                    visibility,
                )
            )
            records.append(
                {
                    "index": index,
                    "requestedDirection": list(rate_set.direction),
                    "nativeUnitDirection": unit_direction,
                    "directionalRateMagnitude": float(rate_set.directionalVelocity),
                    "isotropicRateMagnitude": float(rate_set.isotropicVelocity),
                    "nativeDirectionalVelocity": directional_velocity,
                    "nativeIsotropicVelocity": isotropic_velocity,
                    "requestedMaskMaterials": requested_masks,
                    "maskMaterials": mask_names,
                    "maskMaterialIds": [int(material) for material in mask_materials],
                    "maskSource": "geometry" if requested_masks is None else "request",
                    "requestedCalculateVisibility": bool(rate_set.calculateVisibility),
                    "calculateVisibility": (
                        visibility
                        if float(rate_set.directionalVelocity) > 0.0
                        else "not-applicable: directional velocity is zero"
                    ),
                }
            )
        metadata.update(
            {
                "overload": RATE_SET_OVERLOAD,
                "nativeOverload": RATE_SET_OVERLOAD,
                "rateSetCount": len(records),
                "rateSets": records,
                "masks": [record["maskMaterials"] for record in records],
                "visibility": [record["calculateVisibility"] for record in records],
            }
        )
        return ps.DirectionalProcess(native_sets)

    def _build_material_rates(
        self,
        ps: Any,
        params: DirectionalParams,
        metadata: dict[str, Any],
    ) -> Any:
        requested_direction = list(params.direction or [])
        unit_direction = params.unit_direction()
        etch = params.operation == "etch"
        native_signs = params.rateSigns == "native"
        directional_sign = 1.0 if etch else -1.0
        isotropic_sign = -1.0 if etch else 1.0
        if native_signs:
            directional_sign = 1.0
            isotropic_sign = 1.0

        native_rates: dict[Any, tuple[float, float]] = {}
        rows: list[dict[str, Any]] = []
        for name, rate in (params.materialRates or {}).items():
            handle = material_handle(ps, name)
            native_directional = float(rate.directional) * directional_sign
            native_isotropic = float(rate.isotropic) * isotropic_sign
            native_rates[handle] = (native_directional, native_isotropic)
            rows.append(
                {
                    "name": name,
                    "id": _material_id(handle),
                    "directionalRateMagnitude": float(rate.directional),
                    "isotropicRateMagnitude": float(rate.isotropic),
                    "nativeDirectionalRate": native_directional,
                    "nativeIsotropicRate": native_isotropic,
                }
            )

        default = params.defaultMaterialRate
        default_directional = 0.0 if default is None else float(default.directional)
        default_isotropic = 0.0 if default is None else float(default.isotropic)
        native_default_directional = default_directional * directional_sign
        native_default_isotropic = default_isotropic * isotropic_sign

        model = ps.DirectionalProcess(
            unit_direction,
            native_rates,
            native_default_directional,
            native_default_isotropic,
        )
        metadata.update(
            {
                "overload": MATERIAL_RATE_OVERLOAD,
                "nativeOverload": MATERIAL_RATE_OVERLOAD,
                "requestedDirection": requested_direction,
                "nativeUnitDirection": unit_direction,
                "materialRates": rows,
                "materialTable": {
                    row["name"]: [row["nativeDirectionalRate"], row["nativeIsotropicRate"]] for row in rows
                },
                "nativeMaterialRatesByName": {
                    row["name"]: [row["nativeDirectionalRate"], row["nativeIsotropicRate"]] for row in rows
                },
                "nativeMaterialRatesById": {
                    str(row["id"]): [row["nativeDirectionalRate"], row["nativeIsotropicRate"]] for row in rows
                },
                "defaultMaterialRate": {
                    "directional": default_directional,
                    "isotropic": default_isotropic,
                },
                "defaultMaterialRateOmitted": default is None,
                "nativeDefaultDirectionalRate": native_default_directional,
                "nativeDefaultIsotropicRate": native_default_isotropic,
                "directionalSign": directional_sign,
                "isotropicSign": isotropic_sign,
                "rateSigns": params.rateSigns or "operation",
                "masks": "material rate map only; unlisted materials use the default rate",
                "visibility": "not-applicable: native material map has no visibility control",
                "calculateVisibility": "not-applicable: native material map has no visibility control",
            }
        )
        return model


def _require_project(ctx: ProcessContext) -> Any:
    """Return ``ctx.project`` or raise when a required key is missing."""
    project = ctx.project
    missing = [name for name in _REQUIRED_PROJECT_KEYS if not project.get(name)]
    if missing:
        raise RuntimeError(
            "directional native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
