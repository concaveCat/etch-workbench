"""Native CSV rate-grid variant."""

from __future__ import annotations

import hashlib
from pathlib import Path
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

from ..params import CsvProcessParams

__all__ = ["CsvProcessNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")


class CsvProcessNativeVariant(EngineVariant):
    """Run one native ``ps.CSVFileProcess`` step."""

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
        profiles=(ExecutionProfile(backend="cpu", kernel="CPU_DISK"),),
    )
    implementation_hash: str | None = None
    implementation_files = (("process_models/v2/engines/viennaps.py", getattr(_engine_helpers, "__file__", "")),)

    def validate(self, ctx: ProcessContext, params: CsvProcessParams) -> list[Issue]:
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: CsvProcessParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        asset = ctx.assets.get(params.assetId)
        if asset is None:
            raise RuntimeError(f"csvProcess asset {params.assetId!r} is not present in the run context")
        path = Path(str(asset))
        if not path.is_file():
            raise RuntimeError(f"csvProcess asset copy is missing: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != params.assetId:
            raise RuntimeError(
                f"csvProcess asset SHA-256 {digest!r} does not match the project reference {params.assetId!r}"
            )

        mask_names = sorted(protected_material_names(dict(project), dict(ctx.step)))
        masks = [material_handle(ps, name) for name in mask_names]
        direction = params.unit_direction()
        offset = list(params.offset)
        model = ps.CSVFileProcess(
            str(path),
            direction,
            offset,
            float(params.isotropicComponent),
            float(params.directionalComponent),
            masks,
            bool(params.calculateVisibility),
        )
        model.setInterpolationMode(params.interpolationMode)
        if params.interpolationMode == "IDW":
            model.setIDWNeighbors(int(params.idwNeighbors or 0))
        model.setOffset(offset)

        process = ps.Process(domain, model, float(params.duration))
        execution = apply_execution_profile(ps, process, engine.profile)
        advection, advection_actual = configure_advection(ps, project)
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
            "nativeClass": "ps.CSVFileProcess",
            "nativeRateGridClass": "RateGrid",
            "assetId": params.assetId,
            "fileName": params.fileName,
            "direction": direction,
            "requestedDirection": list(params.direction),
            "offset": offset,
            "isotropicComponent": float(params.isotropicComponent),
            "directionalComponent": float(params.directionalComponent),
            "interpolationMode": params.interpolationMode,
            "idwNeighbors": params.idwNeighbors,
            "maskMaterials": mask_names,
            "calculateVisibility": bool(params.calculateVisibility),
            "duration": float(params.duration),
            "timeStepRatio": float(advection.timeStepRatio),
            "timeStepRatioSource": advection_actual.get("timeStepRatioSource", "solver.timeStepRatio"),
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


def _require_project(ctx: ProcessContext) -> Any:
    project = ctx.project
    missing = [name for name in _REQUIRED_PROJECT_KEYS if not project.get(name)]
    if missing:
        raise RuntimeError(
            "csvProcess native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
