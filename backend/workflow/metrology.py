"""P0-3 2D metrology contracts and deterministic measurement algorithms."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from ..models import Number, StrictModel
from .ids import sha256_hex
from .provenance import EquivalenceEvidenceV1, ProvenanceVerificationV1


ALGORITHM_VERSION = "1"
Hash64 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Id = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class MetricDefinitionV1(StrictModel):
    schemaVersion: Literal[1]
    metricId: Id
    name: Annotated[str, Field(min_length=1, max_length=80)]
    unit: Literal["um", "nm"]
    description: Annotated[str, Field(max_length=500)] = ""


class RegionV1(StrictModel):
    xMin: Number
    xMax: Number

    @model_validator(mode="after")
    def ordered(self) -> "RegionV1":
        if self.xMin >= self.xMax:
            raise ValueError("region xMin must be less than xMax")
        return self


class CDAtHeightParametersV1(StrictModel):
    kind: Literal["cd_at_height"] = "cd_at_height"
    height: Number
    pairSelection: Literal["outermost", "innermost", "first"] = "outermost"


class EtchDepthParametersV1(StrictModel):
    kind: Literal["etch_depth"] = "etch_depth"
    x: Number
    referenceHeight: Number


MetrologyParametersV1 = Annotated[
    CDAtHeightParametersV1 | EtchDepthParametersV1,
    Field(discriminator="kind"),
]


class MetrologyRecipeV1(StrictModel):
    schemaVersion: Literal[1]
    recipeId: Id
    version: Annotated[int, Field(ge=1)]
    metricId: Id
    name: Annotated[str, Field(min_length=1, max_length=80)]
    dimension: Literal[2, 3]
    method: Literal["cd_at_height", "etch_depth"]
    parameters: MetrologyParametersV1
    material: Annotated[str, Field(min_length=1, max_length=80)]
    region: RegionV1 | None = None

    @model_validator(mode="after")
    def method_matches_parameters(self) -> "MetrologyRecipeV1":
        if self.method != self.parameters.kind:
            raise ValueError("method must match parameters.kind")
        return self


class SnapshotPayloadV1(StrictModel):
    """Typed top-level viewer snapshot payload used by measurement/equivalence."""

    schemaVersion: int | None = None
    runId: str | None = None
    stepId: str | None = None
    stepName: str | None = None
    stepIndex: int | None = None
    axisMap: dict[str, int] = Field(default_factory=lambda: {"x": 0, "h": 1, "z": 2})
    coordinateUnit: str = "um"
    gridDelta: float
    dimension: Literal[2, 3]
    points: list[list[float]] = Field(default_factory=list)
    lines: list[list[int]] = Field(default_factory=list)
    triangles: list[list[int]] = Field(default_factory=list)
    cellData: dict[str, list[int]] = Field(default_factory=dict)
    materials: dict[str, dict[str, Any]] = Field(default_factory=dict)
    mesh: dict[str, Any] | None = None
    materialIdsByLevelSetIndex: list[int] = Field(default_factory=list)
    sameMaterialInternalInterfacesHidden: bool = False
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    noOp: bool = False
    measuredGeometry: dict[str, Any] | None = None
    measuredRemoval: dict[str, Any] | None = None
    geometryReadback: dict[str, Any] | None = None
    topology: str | None = None
    surfaceBounds: list[Any] | None = None
    domainReadback: dict[str, Any] | None = None
    materialSurfaceStatus: str | None = None
    materialTops: dict[str, dict[str, Any]] | None = None


MeasurementStatus = Literal[
    "measured",
    "no_intersection",
    "ambiguous",
    "material_not_found",
    "region_empty",
    "unsupported_dimension",
    "unsupported_units",
    "invalid_geometry",
]


class MeasurementResultV1(StrictModel):
    schemaVersion: Literal[1]
    measurementId: Id
    metricId: Id
    recipeId: Id
    recipeVersion: Annotated[int, Field(ge=1)]
    method: Literal["cd_at_height", "etch_depth"]
    algorithmVersion: Annotated[str, Field(min_length=1, max_length=32)]
    sourceRunId: str | None = None
    sourceStepId: str | None = None
    sourceStateId: str | None = None
    sourceSnapshotHash: Hash64
    status: MeasurementStatus
    value: float | None = None
    unit: Literal["um", "nm"]
    reason: str | None = None
    intersections: list[float] = Field(default_factory=list)
    material: str
    region: RegionV1 | None = None
    coordinateSystem: Literal["cartesian_um"] = "cartesian_um"
    uncertainty: float | None = None
    uncertaintyStatus: Literal["not_evaluated", "estimated"] = "not_evaluated"
    bindingStatus: Literal["unbound", "verified", "failed"] = "unbound"
    bindingEvidence: ProvenanceVerificationV1 | None = None
    equivalenceStatus: Literal["not_evaluated", "established", "failed", "inconclusive"] = "not_evaluated"
    equivalenceEvidence: EquivalenceEvidenceV1 | None = None
    createdAt: str | None = None


def snapshot_content_hash(snapshot: dict[str, Any]) -> str:
    """Hash geometry/content fields, not transient artifact URLs or timestamps."""

    payload = {
        "dimension": snapshot.get("dimension"),
        "axisMap": snapshot.get("axisMap"),
        "coordinateUnit": snapshot.get("coordinateUnit"),
        "gridDelta": snapshot.get("gridDelta"),
        "points": snapshot.get("points"),
        "lines": snapshot.get("lines"),
        "cellData": snapshot.get("cellData"),
        "materials": snapshot.get("materials"),
        "materialTops": snapshot.get("materialTops"),
        "measuredRemoval": snapshot.get("measuredRemoval"),
    }
    return sha256_hex(payload)


def _material_id(snapshot: dict[str, Any], material: str) -> int | None:
    for key, value in (snapshot.get("materials") or {}).items():
        if isinstance(value, dict) and value.get("name") == material:
            try:
                return int(key)
            except (TypeError, ValueError):
                return None
    return None


def _axes(snapshot: dict[str, Any]) -> tuple[int, int]:
    axis_map = snapshot.get("axisMap") or {}
    try:
        return int(axis_map.get("x", 0)), int(axis_map.get("h", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("axisMap must contain integer x and h indices") from exc


def _in_region(x: float, region: RegionV1 | None) -> bool:
    return region is None or (region.xMin <= x <= region.xMax)


def _convert(value_um: float, unit: str) -> float:
    return value_um if unit == "um" else value_um * 1000.0


def _result(
    recipe: MetrologyRecipeV1,
    metric: MetricDefinitionV1,
    snapshot: dict[str, Any],
    *,
    source_run_id: str | None,
    source_step_id: str | None,
    source_state_id: str | None,
    status: MeasurementStatus,
    value: float | None = None,
    reason: str | None = None,
    intersections: list[float] | None = None,
) -> MeasurementResultV1:
    return MeasurementResultV1(
        schemaVersion=1,
        measurementId=f"m_{uuid.uuid4().hex}",
        metricId=metric.metricId,
        recipeId=recipe.recipeId,
        recipeVersion=recipe.version,
        method=recipe.method,
        algorithmVersion=ALGORITHM_VERSION,
        sourceRunId=source_run_id,
        sourceStepId=source_step_id,
        sourceStateId=source_state_id,
        sourceSnapshotHash=snapshot_content_hash(snapshot),
        status=status,
        value=value,
        unit=metric.unit,
        reason=reason,
        intersections=intersections or [],
        material=recipe.material,
        region=recipe.region,
        uncertainty=None,
        uncertaintyStatus="not_evaluated",
        createdAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def measure_snapshot(
    recipe: MetrologyRecipeV1,
    metric: MetricDefinitionV1,
    snapshot: dict[str, Any],
    *,
    source_run_id: str | None = None,
    source_step_id: str | None = None,
    source_state_id: str | None = None,
) -> MeasurementResultV1:
    """Measure a 2D snapshot; return an explicit status instead of guessing."""

    if recipe.metricId != metric.metricId:
        raise ValueError("recipe metricId does not match the metric definition")
    if snapshot.get("dimension") == 3:
        if recipe.dimension != 3 or recipe.method != "etch_depth":
            return _result(
                recipe, metric, snapshot,
                source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
                status="unsupported_dimension", reason="3D metrology v1 supports material top-height depth only",
            )
        tops = snapshot.get("materialTops") or {}
        top = tops.get(recipe.material)
        if not isinstance(top, dict):
            return _result(
                recipe, metric, snapshot,
                source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
                status="material_not_found", reason=f"material {recipe.material!r} has no 3D top-height summary",
            )
        height = top.get("medianTopHeight", top.get("topHeight"))
        if not isinstance(height, (int, float)):
            return _result(
                recipe, metric, snapshot,
                source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
                status="no_intersection", reason="3D material top-height summary has no finite height",
            )
        assert isinstance(recipe.parameters, EtchDepthParametersV1)
        depth = recipe.parameters.referenceHeight - float(height)
        return _result(
            recipe, metric, snapshot,
            source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
            status="measured", value=_convert(depth, metric.unit), intersections=[float(height)],
        )
    if snapshot.get("dimension") != 2:
        return _result(
            recipe, metric, snapshot,
            source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
            status="unsupported_dimension", reason="P0-3 metrology supports 2D and 3D-depth snapshots only",
        )
    if snapshot.get("coordinateUnit") != "um":
        return _result(
            recipe, metric, snapshot,
            source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
            status="unsupported_units",
            reason="P0-3 V1 requires snapshot coordinates in um; normalize before measuring",
        )
    points = snapshot.get("points")
    lines = snapshot.get("lines")
    material_ids = (snapshot.get("cellData") or {}).get("materialIds")
    if not isinstance(points, list) or not isinstance(lines, list) or not isinstance(material_ids, list):
        return _result(
            recipe, metric, snapshot,
            source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
            status="invalid_geometry", reason="snapshot requires points, lines and cellData.materialIds",
        )
    if len(lines) != len(material_ids):
        return _result(
            recipe, metric, snapshot,
            source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
            status="invalid_geometry", reason="line and material-id counts differ",
        )
    material_id = _material_id(snapshot, recipe.material)
    if material_id is None:
        return _result(
            recipe, metric, snapshot,
            source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
            status="material_not_found", reason=f"material {recipe.material!r} is not present in the snapshot",
        )
    try:
        x_index, h_index = _axes(snapshot)
    except ValueError as exc:
        return _result(
            recipe, metric, snapshot,
            source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
            status="invalid_geometry", reason=str(exc),
        )

    if recipe.method == "cd_at_height":
        assert isinstance(recipe.parameters, CDAtHeightParametersV1)
        height = recipe.parameters.height
        crossings: list[float] = []
        raw_crossings: list[float] = []
        degenerate = False
        for (first, second), line_material in zip(lines, material_ids):
            if line_material != material_id:
                continue
            try:
                x1, h1 = float(points[first][x_index]), float(points[first][h_index])
                x2, h2 = float(points[second][x_index]), float(points[second][h_index])
            except (IndexError, TypeError, ValueError):
                return _result(
                    recipe, metric, snapshot,
                    source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
                    status="invalid_geometry", reason="snapshot has an invalid point or line index",
                )
            if abs(h2 - h1) <= 1e-12:
                if abs(h1 - height) <= 1e-12:
                    if recipe.region is None or not (max(x1, x2) < recipe.region.xMin or min(x1, x2) > recipe.region.xMax):
                        degenerate = True
                continue
            if min(h1, h2) <= height <= max(h1, h2):
                x = x1 + (height - h1) * (x2 - x1) / (h2 - h1)
                raw_crossings.append(x)
                if _in_region(x, recipe.region):
                    crossings.append(x)
        crossings = sorted({round(value, 12) for value in crossings})
        if degenerate:
            return _result(
                recipe, metric, snapshot,
                source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
                status="ambiguous", reason="a selected-material segment lies on the measurement height",
                intersections=crossings,
            )
        if len(crossings) < 2:
            return _result(
                recipe, metric, snapshot,
                source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
                status="region_empty" if len(set(round(value, 12) for value in raw_crossings)) >= 2 and recipe.region is not None else "no_intersection",
                reason="fewer than two selected-material crossings at the measurement height",
                intersections=crossings,
            )
        if recipe.parameters.pairSelection == "first":
            left, right = crossings[0], crossings[1]
        elif recipe.parameters.pairSelection == "outermost":
            left, right = crossings[0], crossings[-1]
        else:
            gaps = list(zip(crossings, crossings[1:]))
            left, right = min(gaps, key=lambda pair: pair[1] - pair[0])
        return _result(
            recipe, metric, snapshot,
            source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
            status="measured", value=_convert(abs(right - left), metric.unit),
            intersections=crossings,
        )

    assert isinstance(recipe.parameters, EtchDepthParametersV1)
    if recipe.region is not None and not _in_region(recipe.parameters.x, recipe.region):
        return _result(
            recipe, metric, snapshot,
            source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
            status="region_empty", reason="etch-depth x coordinate is outside the measurement region",
        )
    heights: list[float] = []
    for (first, second), line_material in zip(lines, material_ids):
        if line_material != material_id:
            continue
        try:
            x1, h1 = float(points[first][x_index]), float(points[first][h_index])
            x2, h2 = float(points[second][x_index]), float(points[second][h_index])
        except (IndexError, TypeError, ValueError):
            return _result(
                recipe, metric, snapshot,
                source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
                status="invalid_geometry", reason="snapshot has an invalid point or line index",
            )
        x = recipe.parameters.x
        if abs(x2 - x1) <= 1e-12:
            if abs(x - x1) <= 1e-12:
                heights.extend([h1, h2])
            continue
        if min(x1, x2) <= x <= max(x1, x2):
            heights.append(h1 + (x - x1) * (h2 - h1) / (x2 - x1))
    unique = sorted({round(value, 12) for value in heights})
    if not unique:
        return _result(
            recipe, metric, snapshot,
            source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
            status="no_intersection", reason="no selected-material segment crosses the measurement x",
        )
    if len(unique) > 1:
        return _result(
            recipe, metric, snapshot,
            source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
            status="ambiguous", reason="multiple selected-material surfaces cross the measurement x",
            intersections=unique,
        )
    depth = recipe.parameters.referenceHeight - unique[0]
    return _result(
        recipe, metric, snapshot,
        source_run_id=source_run_id, source_step_id=source_step_id, source_state_id=source_state_id,
        status="measured", value=_convert(depth, metric.unit), intersections=unique,
    )
