"""Typed /api/v1 workflow endpoints for P0-2 route plans."""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field, ValidationError, field_serializer, model_validator

from .. import snapshots, storage
from ..models import ProjectV7, StepV7, StrictModel
from .adoption import adopt_project, first_route
from .analysis import (
    build_report,
    candidate_view,
    experiment_csv,
    load_snapshot,
    process_window,
    snapshot_summary,
)
from .analysis import (
    compare_snapshots as compare_analysis_snapshots,
)
from .branches import create_branch
from .calibration import create_dataset, run_fit
from .decisions import (
    MeasurementDecisionRequestV1,
    MeasurementDecisionV1,
    WorkflowRunDetailV1,
    WorkflowRunListResponse,
    create_measurement_decision,
    get_workflow_run,
    list_workflow_runs,
    replay_measurement_decision,
)
from .equivalence import ComparisonProtocolV1, EquivalenceResultV1
from .equivalence import compare_snapshots as compare_equivalence_snapshots
from .execution import execute_segment
from .experiments import (
    apply_experiment_retention,
    create_doe_document,
    create_experiment_document,
    experiment_table,
    resolve_experiment_base,
    retention_preview,
    start_experiment,
)
from .layouts import build_clip_gds, canonical_clip_hash, extract_clip_polygons, save_asset, structure_bounds
from .library import (
    LibraryBundleV1,
    LibraryExportRequestV1,
    LibraryImportResultV1,
    build_bundle,
    import_bundle,
)
from .library_items import (
    MAX_LIBRARY_ITEM_DESCRIPTION,
    MAX_LIBRARY_ITEM_NAME,
    MAX_LIBRARY_ITEMS_PER_CATEGORY,
    LibraryCategory,
    LibraryItemV1,
    create_library_item,
    update_library_item,
)
from .metrology import (
    MeasurementResultV1,
    MetricDefinitionV1,
    MetrologyRecipeV1,
    SnapshotPayloadV1,
    snapshot_content_hash,
)
from .models import CompileDiffV1, CompiledWorkflowV1, GeometryBindingV1
from .modules import (
    ExpandedFlowV1,
    FlowDefinitionV2,
    ModuleV1,
    expand_flow_definition,
    to_flow_revision,
)
from .project_state import ProjectWorkflowStateV1
from .provenance import ProvenanceVerificationV1, VerificationRecordV1
from .routes import AttemptV1, RoutePlanV1, SegmentRefV1, SegmentReuseV1, SegmentV1
from .store import WorkflowConflict, WorkflowError, WorkflowNotFound, WorkflowStore, WorkflowValidation
from .templates import FlowTemplateV1, resolve_template

router = APIRouter(prefix="/api/v1/workflow", tags=["workflow"])


class CompileSummaryV1(StrictModel):
    compileId: str
    flowRevisionId: str
    fullProjectHash: str
    geometryBindingHash: str
    stepCount: Annotated[int, Field(ge=0)]
    createdAt: str


class CompileListResponse(StrictModel):
    compiles: list[CompileSummaryV1]


class RecipeSummaryV1(StrictModel):
    recipeId: str
    version: Annotated[int, Field(ge=1)]
    metricId: str
    method: str
    name: str
    createdAt: str


class RecipeListResponse(StrictModel):
    recipes: list[RecipeSummaryV1]


class MeasureSnapshotRequest(StrictModel):
    recipeId: str
    recipeVersion: Annotated[int, Field(ge=1)]
    snapshot: SnapshotPayloadV1


class MeasureRunRequest(StrictModel):
    recipeId: str
    recipeVersion: Annotated[int, Field(ge=1)]
    runId: str
    stepId: str


class MeasurementListResponse(StrictModel):
    measurements: list[MeasurementResultV1]


class BindMeasurementRequest(StrictModel):
    segmentId: str
    runId: str
    stepId: str


class MeasurementProvenanceResponse(StrictModel):
    measurementId: str
    bindingStatus: str
    bindingEvidence: ProvenanceVerificationV1 | None = None
    verifications: list[VerificationRecordV1] = Field(default_factory=list)


class RouteSummaryV1(StrictModel):
    routeId: str
    flowId: str
    name: str
    headAttemptId: str
    stepCount: Annotated[int, Field(ge=0)]
    attemptCount: Annotated[int, Field(ge=0)]
    legacyUnverified: bool = False
    updatedAt: str


class RouteListResponse(StrictModel):
    routes: list[RouteSummaryV1]


class RoutePlanResponse(StrictModel):
    plan: RoutePlanV1


class AppendAttemptRequest(StrictModel):
    attempt: AttemptV1
    segments: list[SegmentV1]
    reuse: list[SegmentReuseV1] = Field(default_factory=list)


class AttemptDetailResponse(StrictModel):
    attempt: AttemptV1
    segments: list[SegmentV1]
    reuse: list[SegmentReuseV1]


class MaterializeResponse(StrictModel):
    attemptId: str
    segments: list[SegmentRefV1]


class SegmentExecutionResponse(StrictModel):
    segment: SegmentV1
    runId: str


class AdoptProjectRequest(StrictModel):
    project: ProjectV7
    flowId: str | None = None


class AdoptProjectResponse(StrictModel):
    flowId: str
    bindingId: str
    compileId: str
    projectName: str
    dimension: int
    stepCount: int
    authoredSteps: list[StepV7] = Field(default_factory=list)
    binding: GeometryBindingV1 | None = None
    compile: CompiledWorkflowV1

    @field_serializer("authoredSteps")
    def _serialize_authored_steps(self, steps: list[StepV7]) -> list[dict]:
        return [step.model_dump(mode="json", exclude_none=True) for step in steps]


class RouteFromCompileRequest(StrictModel):
    compileId: str
    name: str | None = None
    splitAfterStepId: str | None = None


class EquivalenceCheckRequest(StrictModel):
    protocol: ComparisonProtocolV1
    left: SnapshotPayloadV1
    right: SnapshotPayloadV1
    leftRunId: str | None = None
    rightRunId: str | None = None
    leftSegmentId: str | None = None
    rightSegmentId: str | None = None
    leftStepId: str | None = None
    rightStepId: str | None = None


class ExperimentVariantSpecV1(StrictModel):
    parameters: dict[str, dict] = Field(default_factory=dict)
    replicates: Annotated[int, Field(ge=1, le=8)] = 1
    seed: int = 20261029


class CreateExperimentRequest(StrictModel):
    name: str
    project: ProjectV7
    flowId: str
    compileId: str | None = None
    recipeId: str
    recipeVersion: int
    variants: Annotated[list[ExperimentVariantSpecV1], Field(min_length=1, max_length=256)]
    maxAttempts: Annotated[int, Field(ge=1, le=4)] = 1
    costPerRunUsd: Annotated[float, Field(ge=0, le=1_000_000)] = 0.0


class DoeFactorV1(StrictModel):
    stepId: str
    parameter: str
    values: Annotated[list[float], Field(min_length=2, max_length=10)]


class DoeRequest(StrictModel):
    name: str
    project: ProjectV7
    flowId: str
    compileId: str | None = None
    recipeId: str
    recipeVersion: int
    factors: Annotated[list[DoeFactorV1], Field(min_length=1, max_length=5)]
    replicates: Annotated[int, Field(ge=1, le=8)] = 3
    seed: int = 20261029
    maxAttempts: Annotated[int, Field(ge=1, le=4)] = 2
    costPerRunUsd: Annotated[float, Field(ge=0, le=1_000_000)] = 0.0


class LayoutBoundsV1(StrictModel):
    x0: float
    y0: float
    x1: float
    y1: float


class LayoutStructureV1(StrictModel):
    name: str
    layers: list[int]
    layerDatatypes: list[list[int]]
    maskLayerDatatypes: list[list[int]]
    references: list[str]
    bounds: LayoutBoundsV1 | None = None


class LayoutUnitsV1(StrictModel):
    userUnitsPerDatabaseUnit: float
    metersPerDatabaseUnit: float
    micrometersPerNativeUserUnit: float
    nativeOutputAssumption: str


class LayoutV1(StrictModel):
    schemaVersion: Literal[1]
    layoutId: str
    assetId: str
    fileName: str
    sizeBytes: int
    units: LayoutUnitsV1
    structures: list[LayoutStructureV1]
    createdAt: str


class LayoutRegisterRequest(StrictModel):
    assetId: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    fileName: Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[^/\\]+$")]


class LayoutSummaryV1(StrictModel):
    layoutId: str
    fileName: str
    structureCount: int
    createdAt: str


class LayoutListResponse(StrictModel):
    layouts: list[LayoutSummaryV1]


class ClipBoundsV1(StrictModel):
    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def ordered_bounds(self) -> "ClipBoundsV1":
        if not all(abs(value) <= 1_000_000 for value in (self.x0, self.y0, self.x1, self.y1)):
            raise ValueError("clip bounds must stay within ±1,000,000 µm")
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("clip bounds must satisfy x0 < x1 and y0 < y1")
        return self


class ClipLayerV1(StrictModel):
    """One GDS layer→material mapping; 3D clips also carry a z placement."""

    layer: Annotated[int, Field(ge=0, le=65535)]
    datatype: Annotated[int, Field(ge=0, le=32767)] | None = None
    material: str = "Si"
    # 2D clips omit both fields. 3D clips must supply them (checked on compile,
    # where the grid — and therefore the minimum thickness — is known).
    baseHeight: float | None = None
    height: Annotated[float, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def layer_height_pair(self) -> "ClipLayerV1":
        if (self.baseHeight is None) != (self.height is None):
            raise ValueError("baseHeight and height must be provided together")
        for value in (self.baseHeight, self.height):
            if value is not None and not math.isfinite(value):
                raise ValueError("baseHeight and height must be finite")
        return self


class ClipAnnotationV1(StrictModel):
    annotationId: str | None = None
    text: Annotated[str, Field(min_length=1, max_length=200)]
    x: float
    y: float


class LayoutSelectionV1(StrictModel):
    structure: Annotated[str, Field(min_length=1, max_length=512)]
    layers: Annotated[list[ClipLayerV1], Field(min_length=1, max_length=32)]
    bbox: ClipBoundsV1


class ClipPreviewRequest(LayoutSelectionV1):
    pass


class ClipPolygonV1(StrictModel):
    layer: int
    datatype: int
    points: list[list[float]]


class ClipContentBoundsV1(StrictModel):
    x0: float
    y0: float
    x1: float
    y1: float


class ClipPreviewResponse(StrictModel):
    structure: str
    bbox: ClipBoundsV1
    width: float
    height: float
    polygons: list[ClipPolygonV1]
    polygonCount: int
    pointCount: int
    contentBounds: ClipContentBoundsV1


class ClipCreateRequest(LayoutSelectionV1):
    layoutId: str
    name: Annotated[str, Field(min_length=1, max_length=80)]
    annotations: list[ClipAnnotationV1] = Field(default_factory=list)


class ClipV1(StrictModel):
    schemaVersion: Literal[1]
    clipId: str
    name: str
    layoutId: str
    assetId: str
    structure: str
    layers: list[ClipLayerV1]
    bbox: ClipBoundsV1
    annotations: list[ClipAnnotationV1]
    polygons: list[ClipPolygonV1]
    polygonCount: int
    pointCount: int
    contentBounds: ClipContentBoundsV1
    width: float
    height: float
    clipHash: str
    gdsAssetId: str
    gdsFileName: str
    createdAt: str


class ClipListResponse(StrictModel):
    clips: list[ClipV1]


class ClipAnnotationRequest(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=200)]
    x: float
    y: float


class ClipCompileRequest(StrictModel):
    project: ProjectV7
    flowId: str | None = None
    gridDelta: Annotated[float, Field(gt=0, le=1)] | None = None


class TemplateSummaryV1(StrictModel):
    templateId: str
    name: str
    stepCount: int
    createdAt: str


class TemplateListResponse(StrictModel):
    templates: list[TemplateSummaryV1]


class ModuleSummaryV1(StrictModel):
    moduleId: str
    name: str
    revision: Annotated[int, Field(ge=1)]
    parameterCount: Annotated[int, Field(ge=0)]
    stepCount: Annotated[int, Field(ge=0)]
    updatedAt: str


class ModuleListResponse(StrictModel):
    modules: list[ModuleSummaryV1]


class FlowDefinitionSummaryV1(StrictModel):
    definitionId: str
    flowId: str
    revision: Annotated[int, Field(ge=1)]
    name: str
    nodeCount: Annotated[int, Field(ge=0)]
    variableCount: Annotated[int, Field(ge=0)]
    updatedAt: str


class FlowDefinitionListResponse(StrictModel):
    definitions: list[FlowDefinitionSummaryV1]


class LibraryBundleSummaryV1(StrictModel):
    bundleId: str
    name: str
    description: str = ""
    createdAt: str
    moduleCount: Annotated[int, Field(ge=0)]
    definitionCount: Annotated[int, Field(ge=0)]
    templateCount: Annotated[int, Field(ge=0)]
    experimentTemplateCount: Annotated[int, Field(ge=0)]


class LibraryBundleListResponse(StrictModel):
    bundles: list[LibraryBundleSummaryV1]


class LibraryBundleImportRequest(StrictModel):
    policy: Literal["reject", "clone"] = "reject"


class LibraryDirectImportRequest(StrictModel):
    bundle: LibraryBundleV1
    policy: Literal["reject", "clone"] = "reject"


class LibraryItemCreateRequest(StrictModel):
    """Create one reusable library object; ``itemId`` is server-generated."""

    category: LibraryCategory
    name: Annotated[str, Field(min_length=1, max_length=MAX_LIBRARY_ITEM_NAME)]
    description: Annotated[str, Field(max_length=MAX_LIBRARY_ITEM_DESCRIPTION)] = ""
    payload: dict[str, Any]


class LibraryItemUpdateRequest(StrictModel):
    """Replace the editable fields of an item; category and id are immutable."""

    name: Annotated[str, Field(min_length=1, max_length=MAX_LIBRARY_ITEM_NAME)]
    description: Annotated[str, Field(max_length=MAX_LIBRARY_ITEM_DESCRIPTION)] = ""
    payload: dict[str, Any]


class LibraryItemListResponse(StrictModel):
    items: list[LibraryItemV1]


ProjectId = Annotated[str, Field(pattern=r"^proj_[a-f0-9]{24}$")]


class ProjectRecordV1(StrictModel):
    """Summary of one persisted project; the document itself is ``ProjectV7``."""

    schemaVersion: Literal[1] = 1
    projectId: ProjectId
    name: Annotated[str, Field(min_length=1, max_length=80)]
    dimension: Literal[2, 3]
    stepCount: Annotated[int, Field(ge=0)]
    createdAt: str
    updatedAt: str


class ProjectListResponse(StrictModel):
    projects: list[ProjectRecordV1]


class ProjectNormalizeResponse(StrictModel):
    """The server's canonical form of a project document (no persistence)."""

    project: ProjectV7


class ExpandFlowDefinitionRequest(StrictModel):
    variables: dict[str, float | int | str | bool] = Field(default_factory=dict)


class CompileFlowDefinitionRequest(StrictModel):
    project: ProjectV7
    flowId: str | None = None
    variables: dict[str, float | int | str | bool] = Field(default_factory=dict)


class BranchRequest(StrictModel):
    compileId: str
    startAfterStepId: str
    changedStepIds: list[str] = Field(default_factory=list)


class MeasurementDecisionResponse(StrictModel):
    decision: MeasurementDecisionV1
    plan: RoutePlanV1
    workflowRunId: str
    measurement: MeasurementResultV1
    resolvedParameters: dict[str, dict[str, object]]


class MeasurementDecisionListResponse(StrictModel):
    decisions: list[MeasurementDecisionV1]


class FromTemplateRequest(StrictModel):
    project: ProjectV7
    templateId: str
    flowId: str | None = None
    variables: dict[str, float | int | str] = Field(default_factory=dict)
    mode: Literal["replace", "append"] = "replace"


class AnalysisCandidateV1(StrictModel):
    runId: str
    stepId: str
    label: str | None = None


class AnalysisCompareRequest(StrictModel):
    candidates: Annotated[list[AnalysisCandidateV1], Field(min_length=2, max_length=6)]
    tolerance: Annotated[float, Field(ge=0, le=1_000_000)] = 1e-6
    materialTolerance: Annotated[float, Field(ge=0, le=1_000_000)] | None = None


class ProcessWindowRequest(StrictModel):
    experimentId: str
    lower: float
    upper: float

    @model_validator(mode="after")
    def ordered_limits(self) -> "ProcessWindowRequest":
        if not self.lower < self.upper:
            raise ValueError("process window lower must be smaller than upper")
        return self


class ReportRequest(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=120)] = "Workflow report"
    projectId: str | None = None
    experimentId: str | None = None
    runId: str | None = None
    stepId: str | None = None
    measurementId: str | None = None
    conclusions: Annotated[str, Field(max_length=20_000)] = ""
    lower: float | None = None
    upper: float | None = None

    @model_validator(mode="after")
    def require_source(self) -> "ReportRequest":
        if not (self.runId or self.measurementId or self.experimentId):
            raise ValueError("报告需要至少一次运行、测量或实验")
        return self


class ReportResponse(StrictModel):
    reportId: str
    title: str
    html: str
    sha256: str
    createdAt: str


class CalibrationDatasetRequest(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    csv: Annotated[str, Field(min_length=1, max_length=2_000_000)]


class CalibrationFitRequest(StrictModel):
    datasetId: str
    model: Literal["scale", "scale_offset", "quadratic", "power", "power_offset"]
    method: Literal["least-squares", "nelder-mead"]


class ExperimentRetentionRequest(StrictModel):
    mode: Literal["keep-all", "release-runs"]


class ProtocolSummaryV1(StrictModel):
    protocolId: str
    version: int
    methodVersion: str | None = None
    createdAt: str


class ProtocolListResponse(StrictModel):
    protocols: list[ProtocolSummaryV1]


class GcDryRunResponse(StrictModel):
    referenced: list[str]
    candidates: list[dict]
    bytesReclaimable: int


class ArtifactRefRequest(StrictModel):
    artifactId: str
    ownerType: str
    ownerId: str
    requiredFor: str


class ArtifactRefItemV1(StrictModel):
    artifactId: str
    ownerType: str
    ownerId: str
    requiredFor: str


class ArtifactRefResponse(StrictModel):
    refs: list[ArtifactRefItemV1]


class AbandonRequest(StrictModel):
    artifactId: str
    ownerType: str
    ownerId: str
    capability: str
    reason: str


@lru_cache(maxsize=8)
def _store_for(var: str) -> WorkflowStore:
    store = WorkflowStore(Path(var) / "workflow" / "workflow.sqlite3")
    store.initialize()
    return store


def get_store() -> WorkflowStore:
    storage.initialize()
    return _store_for(str(storage.VAR))


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkflowNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, WorkflowConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, WorkflowValidation):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ValidationError):
        return HTTPException(status_code=422, detail=exc.errors(include_url=False, include_context=False, include_input=False))
    return HTTPException(status_code=500, detail="workflow persistence failed")


def _call(operation):
    try:
        return operation()
    except (WorkflowError, ValidationError) as exc:
        raise _http_error(exc) from exc


@router.post("/analysis/compare", response_model=dict)
async def analyze_compare(request: AnalysisCompareRequest) -> dict:
    def operation():
        candidates = []
        snapshots = []
        for candidate in request.candidates:
            try:
                snapshot = load_snapshot(candidate.runId, candidate.stepId)
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise WorkflowNotFound(f"snapshot for run {candidate.runId!r} step {candidate.stepId!r} is unavailable") from exc
            snapshots.append(snapshot)
            candidates.append({
                "runId": candidate.runId, "stepId": candidate.stepId,
                "label": candidate.label, "summary": snapshot_summary(snapshot),
            })
        pairwise = []
        for left in range(len(snapshots)):
            for right in range(left + 1, len(snapshots)):
                comparison = compare_analysis_snapshots(
                    snapshots[left], snapshots[right], request.tolerance, request.materialTolerance,
                )
                comparison["left"] = request.candidates[left].label or request.candidates[left].runId
                comparison["right"] = request.candidates[right].label or request.candidates[right].runId
                pairwise.append(comparison)
        return {
            "candidates": candidates, "pairwise": pairwise,
            "tolerance": request.tolerance, "materialTolerance": request.materialTolerance,
        }
    return _call(operation)


@router.get("/analysis/candidates/{run_id}/{step_id}/view", response_model=dict)
async def analyze_candidate_view(run_id: str, step_id: str) -> dict:
    def operation():
        try:
            return candidate_view(load_snapshot(run_id, step_id))
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise WorkflowNotFound(f"snapshot for run {run_id!r} step {step_id!r} is unavailable") from exc
    return _call(operation)


@router.post("/analysis/process-window", response_model=dict)
async def analyze_process_window(request: ProcessWindowRequest) -> dict:
    def operation():
        document = get_store().get_experiment(request.experimentId)
        return process_window(document, request.lower, request.upper)
    return _call(operation)


@router.post("/analysis/report", response_model=ReportResponse)
async def analyze_report(request: ReportRequest) -> ReportResponse:
    def operation():
        store = get_store()
        result = build_report(store, request.model_dump(mode="json"))
        if request.projectId:
            store.save_report(
                request.projectId,
                result["reportId"],
                result["title"],
                result["html"],
                result["sha256"],
                result["createdAt"],
            )
        return result
    return _call(operation)


@router.post("/calibration/datasets", response_model=dict, status_code=201)
async def create_calibration_dataset(request: CalibrationDatasetRequest) -> dict:
    def operation():
        store = get_store()
        document = create_dataset(request.name, request.csv)
        store.save_calibration_dataset(document)
        return document
    try:
        return _call(operation)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/calibration/datasets/{dataset_id}", response_model=dict)
async def get_calibration_dataset(dataset_id: str) -> dict:
    return _call(lambda: get_store().get_calibration_dataset(dataset_id))


@router.post("/calibration/fit", response_model=dict, status_code=201)
async def fit_calibration(request: CalibrationFitRequest) -> dict:
    def operation():
        store = get_store()
        dataset = store.get_calibration_dataset(request.datasetId)
        result = run_fit(dataset, request.model, request.method)
        store.save_calibration_fit(result)
        return result
    try:
        return _call(operation)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/calibration/fits/{fit_id}", response_model=dict)
async def get_calibration_fit(fit_id: str) -> dict:
    return _call(lambda: get_store().get_calibration_fit(fit_id))


@router.post("/comparison-protocols", response_model=ComparisonProtocolV1, status_code=201)
async def register_protocol(protocol: ComparisonProtocolV1) -> ComparisonProtocolV1:
    return _call(lambda: get_store().register_comparison_protocol(protocol))


@router.get("/comparison-protocols", response_model=ProtocolListResponse)
async def list_protocols() -> ProtocolListResponse:
    rows = _call(get_store().list_comparison_protocols)
    return ProtocolListResponse(protocols=[ProtocolSummaryV1.model_validate(row) for row in rows])


@router.get("/comparison-protocols/{protocol_id}", response_model=ComparisonProtocolV1)
async def get_protocol(protocol_id: str, version: int | None = None) -> ComparisonProtocolV1:
    return _call(lambda: get_store().get_comparison_protocol(protocol_id, version))


@router.get("/equivalence/{check_id}", response_model=EquivalenceResultV1)
async def get_equivalence(check_id: str) -> EquivalenceResultV1:
    return _call(lambda: get_store().get_equivalence(check_id))


@router.post("/artifact-refs", response_model=dict, status_code=201)
async def register_artifact_ref(request: ArtifactRefRequest) -> dict:
    _call(lambda: get_store().register_artifact_ref(request.artifactId, request.ownerType, request.ownerId, request.requiredFor))
    return {"registered": True}


@router.get("/artifact-refs", response_model=ArtifactRefResponse)
async def list_artifact_refs(artifact_id: str | None = None) -> ArtifactRefResponse:
    rows = _call(lambda: get_store().list_artifact_refs(artifact_id))
    return ArtifactRefResponse(refs=[ArtifactRefItemV1.model_validate(row) for row in rows])


@router.post("/gc/abandon", response_model=dict)
async def abandon_capability(request: AbandonRequest) -> dict:
    return _call(lambda: get_store().abandon_capability(
        request.artifactId, request.ownerType, request.ownerId, request.capability, request.reason,
    ))


@router.post("/gc/dry-run", response_model=GcDryRunResponse)
async def gc_dry_run() -> GcDryRunResponse:
    return GcDryRunResponse.model_validate(_call(get_store().gc_dry_run))


@router.post("/artifacts/{artifact_id}/pin", response_model=dict)
async def pin_artifact(artifact_id: str) -> dict:
    _call(lambda: get_store().pin_artifact(artifact_id))
    return {"artifactId": artifact_id, "pinned": True}


@router.post("/artifacts/{artifact_id}/unpin", response_model=dict)
async def unpin_artifact(artifact_id: str) -> dict:
    _call(lambda: get_store().unpin_artifact(artifact_id))
    return {"artifactId": artifact_id, "pinned": False}


def _load_run_snapshot(run_id: str, step_id: str) -> tuple[dict | None, dict | None, str | None]:
    try:
        run_dir = storage.run_path(run_id)
        manifest = storage.read_json(run_dir / "manifest.json")
        row = next((item for item in manifest.get("snapshots", []) if item.get("stepId") == step_id), None)
        if row is None:
            return None, manifest, f"run {run_id!r} has no snapshot for step {step_id!r}"
        path = snapshots.ensure_materialized(run_dir, step_id)
        return storage.read_json(path), manifest, None
    except (FileNotFoundError, OSError, ValueError) as exc:
        return None, None, f"run {run_id!r} snapshot is unavailable: {exc}"


@router.post("/equivalence/check", response_model=EquivalenceResultV1)
async def equivalence_check(request: EquivalenceCheckRequest) -> EquivalenceResultV1:
    def operation():
        store = get_store()
        store.register_comparison_protocol(request.protocol)
        left_payload = request.left.model_dump(mode="json", exclude_none=True)
        right_payload = request.right.model_dump(mode="json", exclude_none=True)
        result = compare_equivalence_snapshots(request.protocol, left_payload, right_payload).model_copy(update={
            "leftRunId": request.leftRunId,
            "rightRunId": request.rightRunId,
            "leftSegmentId": request.leftSegmentId,
            "rightSegmentId": request.rightSegmentId,
            "leftStepId": request.leftStepId,
            "rightStepId": request.rightStepId,
        })
        reasons: list[str] = []
        failed = False
        if request.protocol.checkType != "snapshot":
            required = (request.leftRunId, request.rightRunId, request.leftSegmentId,
                        request.rightSegmentId, request.leftStepId, request.rightStepId)
            if any(value is None for value in required):
                reasons.append("checkType requires both run/segment/step identities")
            else:
                left_actual, left_manifest, left_error = _load_run_snapshot(request.leftRunId, request.leftStepId)
                right_actual, right_manifest, right_error = _load_run_snapshot(request.rightRunId, request.rightStepId)
                if left_error:
                    reasons.append(left_error)
                if right_error:
                    reasons.append(right_error)
                if left_actual is not None and snapshot_content_hash(left_payload) != snapshot_content_hash(left_actual):
                    reasons.append("left request snapshot does not match the actual run snapshot")
                    failed = True
                if right_actual is not None and snapshot_content_hash(right_payload) != snapshot_content_hash(right_actual):
                    reasons.append("right request snapshot does not match the actual run snapshot")
                    failed = True
                try:
                    left_segment = store.get_segment(request.leftSegmentId)
                    right_segment = store.get_segment(request.rightSegmentId)
                except Exception as exc:
                    reasons.append(f"segment identity is unavailable: {exc}")
                    left_segment = right_segment = None
                if left_segment is not None and right_segment is not None:
                    if request.protocol.checkType == "same_segment_replay":
                        if left_segment.segmentId != right_segment.segmentId:
                            reasons.append("same_segment_replay requires the same Segment")
                        if left_segment.runId != request.leftRunId:
                            reasons.append("left run is not owned by the selected Segment")
                        if right_segment.runId != request.rightRunId:
                            if left_manifest is None or right_manifest is None or left_manifest.get("requestedInputHash") != right_manifest.get("requestedInputHash"):
                                reasons.append("right run is neither the Segment run nor a replay of the same input")
                    else:
                        if left_segment.segmentId == right_segment.segmentId:
                            reasons.append(f"{request.protocol.checkType} requires distinct Segments")
                        if request.protocol.checkType == "prefix_reuse":
                            if right_segment.startAfterStepId is None or right_segment.startCheckpointRef is None:
                                reasons.append("prefix_reuse right Segment has no checkpoint boundary")
                            elif right_segment.startCheckpointRef.runId != request.rightRunId:
                                reasons.append("prefix_reuse right Segment does not start from the right run")
                            elif left_segment.throughStepId != right_segment.startAfterStepId:
                                reasons.append("prefix_reuse left Segment does not end at the right Segment boundary")
                            if left_segment.runId != request.leftRunId:
                                reasons.append("left run is not owned by the left Segment")
                        elif left_segment.runId != request.leftRunId or right_segment.runId != request.rightRunId:
                            reasons.append("one or both runs are not owned by their Segments")
        if reasons:
            result = result.model_copy(update={"status": "failed" if failed else "inconclusive", "reasons": reasons})
        return store.record_equivalence(result)
    return _call(operation)


@router.post("/metrics", response_model=MetricDefinitionV1, status_code=201)
async def register_metric(metric: MetricDefinitionV1) -> MetricDefinitionV1:
    return _call(lambda: get_store().register_metric(metric))


@router.get("/metrics/{metric_id}", response_model=MetricDefinitionV1)
async def get_metric(metric_id: str) -> MetricDefinitionV1:
    return _call(lambda: get_store().get_metric(metric_id))


@router.post("/metrology-recipes", response_model=MetrologyRecipeV1, status_code=201)
async def register_recipe(recipe: MetrologyRecipeV1) -> MetrologyRecipeV1:
    return _call(lambda: get_store().register_recipe(recipe))


@router.get("/metrology-recipes", response_model=RecipeListResponse)
async def list_recipes() -> RecipeListResponse:
    rows = _call(get_store().list_recipes)
    return RecipeListResponse(recipes=[RecipeSummaryV1.model_validate(row) for row in rows])


@router.get("/metrology-recipes/{recipe_id}", response_model=MetrologyRecipeV1)
async def get_recipe(recipe_id: str, version: int | None = None) -> MetrologyRecipeV1:
    return _call(lambda: get_store().get_recipe(recipe_id, version))


@router.post("/measurements", response_model=MeasurementResultV1, status_code=201)
async def measure_snapshot(request: MeasureSnapshotRequest) -> MeasurementResultV1:
    def operation():
        store = get_store()
        recipe = store.get_recipe(request.recipeId, request.recipeVersion)
        metric = store.get_metric(recipe.metricId)
        return store.measure_and_record(recipe, metric, request.snapshot.model_dump(mode="json", exclude_none=True))
    return _call(operation)


@router.post("/measurements/from-run", response_model=MeasurementResultV1, status_code=201)
async def measure_run(request: MeasureRunRequest) -> MeasurementResultV1:
    def operation():
        store = get_store()
        recipe = store.get_recipe(request.recipeId, request.recipeVersion)
        metric = store.get_metric(recipe.metricId)
        return store.measure_run(recipe, metric, request.runId, request.stepId)
    return _call(operation)


@router.get("/measurements", response_model=MeasurementListResponse)
async def list_measurements(
    run_id: str | None = None,
    recipe_id: str | None = None,
    binding_status: str | None = None,
) -> MeasurementListResponse:
    rows = _call(
        lambda: get_store().list_measurements(
            run_id=run_id,
            recipe_id=recipe_id,
            binding_status=binding_status,
        )
    )
    return MeasurementListResponse(measurements=rows)


@router.post("/measurements/{measurement_id}/bind", response_model=MeasurementResultV1)
async def bind_measurement(measurement_id: str, request: BindMeasurementRequest) -> MeasurementResultV1:
    return _call(
        lambda: get_store().bind_measurement(
            measurement_id,
            request.segmentId,
            request.runId,
            request.stepId,
        )
    )


@router.get("/measurements/{measurement_id}/provenance", response_model=MeasurementProvenanceResponse)
async def measurement_provenance(measurement_id: str) -> MeasurementProvenanceResponse:
    store = get_store()
    measurement = _call(lambda: store.get_measurement(measurement_id))
    verifications = _call(lambda: store.list_verifications(measurement_id))
    return MeasurementProvenanceResponse(
        measurementId=measurement.measurementId,
        bindingStatus=measurement.bindingStatus,
        bindingEvidence=measurement.bindingEvidence,
        verifications=verifications,
    )


@router.get("/measurements/{measurement_id}", response_model=MeasurementResultV1)
async def get_measurement(measurement_id: str) -> MeasurementResultV1:
    return _call(lambda: get_store().get_measurement(measurement_id))


@router.post("/compiles", response_model=CompiledWorkflowV1, status_code=201)
async def register_compile(compiled: CompiledWorkflowV1) -> CompiledWorkflowV1:
    return _call(lambda: get_store().register_compile(compiled))


@router.get("/compiles", response_model=CompileListResponse)
async def list_compiles() -> CompileListResponse:
    rows = _call(get_store().list_compiles)
    return CompileListResponse(compiles=[CompileSummaryV1.model_validate(row) for row in rows])


@router.get("/geometry-bindings/{binding_id}", response_model=GeometryBindingV1)
async def get_geometry_binding(binding_id: str) -> GeometryBindingV1:
    return _call(lambda: get_store().get_binding(binding_id))


@router.get("/compiles/{compile_id}", response_model=CompiledWorkflowV1)
async def get_compile(compile_id: str) -> CompiledWorkflowV1:
    return _call(lambda: get_store().get_compile(compile_id))


@router.post("/flows/from-project", response_model=AdoptProjectResponse, status_code=201)
async def adopt_project_endpoint(request: AdoptProjectRequest) -> AdoptProjectResponse:
    def operation():
        flow, binding, compiled = adopt_project(request.project.model_dump(mode="json", exclude_none=True), flow_id=request.flowId)
        get_store().register_binding(binding)
        get_store().register_compile(compiled)
        return AdoptProjectResponse(
            flowId=flow.flowId,
            bindingId=binding.bindingId,
            compileId=compiled.compileRecord.compileId,
            projectName=compiled.project.name,
            dimension=compiled.project.dimension,
            stepCount=len(compiled.project.steps),
            authoredSteps=flow.steps,
            binding=binding,
            compile=compiled,
        )
    return _call(operation)


@router.post("/routes/from-compile", response_model=RoutePlanResponse, status_code=201)
async def route_from_compile(request: RouteFromCompileRequest) -> RoutePlanResponse:
    def operation():
        compiled = get_store().get_compile(request.compileId)
        plan = first_route(compiled, name=request.name, split_after_step_id=request.splitAfterStepId)
        get_store().create_plan(plan)
        return RoutePlanResponse(plan=plan)
    return _call(operation)


@router.post("/experiments", response_model=dict, status_code=201)
async def create_experiment(request: CreateExperimentRequest) -> dict:
    def operation():
        store = get_store()
        recipe = store.get_recipe(request.recipeId, request.recipeVersion)
        metric = store.get_metric(recipe.metricId)
        project, flow_id, base_compile_id, binding = resolve_experiment_base(
            store, request.project.model_dump(mode="json", exclude_none=True),
            request.flowId, request.compileId,
        )
        document = create_experiment_document(
            name=request.name,
            project=project,
            flow_id=flow_id,
            recipe_id=request.recipeId,
            recipe_version=request.recipeVersion,
            variants=[item.model_dump(mode="json") for item in request.variants],
            metric_id=metric.metricId, unit=metric.unit,
            max_attempts=request.maxAttempts, cost_per_run_usd=request.costPerRunUsd,
            base_compile_id=base_compile_id, binding=binding,
        )
        return store.save_experiment(document["experimentId"], document)
    return _call(operation)


@router.post("/experiments/doe", response_model=dict, status_code=201)
async def create_doe(request: DoeRequest) -> dict:
    def operation():
        store = get_store()
        recipe = store.get_recipe(request.recipeId, request.recipeVersion)
        metric = store.get_metric(recipe.metricId)
        project, flow_id, base_compile_id, binding = resolve_experiment_base(
            store, request.project.model_dump(mode="json", exclude_none=True),
            request.flowId, request.compileId,
        )
        try:
            document = create_doe_document(
                name=request.name,
                project=project,
                flow_id=flow_id,
                recipe_id=request.recipeId,
                recipe_version=request.recipeVersion,
                factors=[item.model_dump(mode="json") for item in request.factors],
                replicates=request.replicates,
                seed=request.seed,
                metric_id=metric.metricId,
                unit=metric.unit,
                max_attempts=request.maxAttempts,
                cost_per_run_usd=request.costPerRunUsd,
                base_compile_id=base_compile_id,
                binding=binding,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return store.save_experiment(document["experimentId"], document)
    return _call(operation)


@router.get("/experiments/{experiment_id}/aggregate", response_model=dict)
async def get_experiment_aggregate(experiment_id: str) -> dict:
    document = _call(lambda: get_store().get_experiment(experiment_id))
    return document.get("aggregation") or {"rows": []}


@router.post("/experiments/{experiment_id}/retention/preview", response_model=dict)
async def preview_experiment_retention(experiment_id: str, request: ExperimentRetentionRequest) -> dict:
    def operation():
        store = get_store()
        document = store.get_experiment(experiment_id)
        return retention_preview(store, document)
    return _call(operation)


@router.post("/experiments/{experiment_id}/retention", response_model=dict)
async def update_experiment_retention(experiment_id: str, request: ExperimentRetentionRequest) -> dict:
    def operation():
        store = get_store()
        document = store.get_experiment(experiment_id)
        return apply_experiment_retention(store, document, request.mode)
    return _call(operation)


@router.get("/experiments/{experiment_id}/table", response_model=dict)
async def get_experiment_table(
    experiment_id: str,
    min_value: float | None = None,
    max_value: float | None = None,
    sort: str = "ordinal",
    order: str = "asc",
) -> dict:
    document = _call(lambda: get_store().get_experiment(experiment_id))
    return experiment_table(document, min_value=min_value, max_value=max_value, sort=sort, order=order)


@router.get("/experiments/{experiment_id}/export.csv", response_class=Response)
async def export_experiment_csv(experiment_id: str) -> Response:
    store = get_store()
    document = _call(lambda: store.get_experiment(experiment_id))
    return Response(
        content=experiment_csv(document, store), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{experiment_id}.csv"'},
    )


@router.get("/experiments", response_model=dict)
async def list_experiments() -> dict:
    return {"experiments": _call(get_store().list_experiments)}


@router.get("/experiments/{experiment_id}", response_model=dict)
async def get_experiment(experiment_id: str) -> dict:
    return _call(lambda: get_store().get_experiment(experiment_id))


@router.delete("/experiments/{experiment_id}", status_code=204)
async def delete_experiment(experiment_id: str) -> Response:
    """Delete one experiment document; runs and measurements stay intact."""

    _call(lambda: get_store().delete_experiment(experiment_id))
    return Response(status_code=204)


@router.post("/experiments/{experiment_id}/run", response_model=dict)
async def run_experiment(experiment_id: str, request: Request) -> dict:
    supervisor = getattr(request.app.state, "supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail="supervisor is unavailable")
    return _call(lambda: start_experiment(get_store(), supervisor, experiment_id))


@router.post("/projects", response_model=ProjectRecordV1, status_code=201)
async def post_project(request: ProjectV7, response: Response) -> ProjectRecordV1:
    """Persist one project document; nothing is compiled or routed."""

    record, created = _call(
        lambda: get_store().create_project(
            request.model_dump(mode="json", exclude_none=True)
        )
    )
    if not created:
        # Same name and identical content: report the stored record instead of
        # creating a duplicate row.
        response.status_code = 200
    return ProjectRecordV1.model_validate(record)


@router.post(
    "/projects/normalize",
    response_model=ProjectNormalizeResponse,
    response_model_exclude_none=True,
)
async def normalize_project(request: ProjectV7) -> ProjectNormalizeResponse:
    """Return the canonical, default-completed form of a project document.

    Pydantic validates the body exactly like ``POST /projects`` does, so the
    answer is what a save would store: every omitted default (``maskMaterial``,
    Fin ``taperAngle``/``maskHeight``, ``stack.baseHeight``, ``step.rateMode``,
    solver defaults …) is filled in. Nothing is written to the store and
    nothing is compiled — the 03 page only needs the normalized document to
    fingerprint the authored project after the server completed it.
    """

    return ProjectNormalizeResponse(project=request)


@router.get("/projects", response_model=ProjectListResponse)
async def get_projects() -> ProjectListResponse:
    rows = _call(get_store().list_projects)
    return ProjectListResponse(
        projects=[ProjectRecordV1.model_validate(row) for row in rows]
    )


@router.get("/projects/{project_id}", response_model=ProjectV7, response_model_exclude_none=True)
async def get_project(project_id: str) -> ProjectV7:
    return ProjectV7.model_validate(_call(lambda: get_store().get_project(project_id)))


@router.put("/projects/{project_id}", response_model=ProjectRecordV1)
async def put_project(project_id: str, request: ProjectV7) -> ProjectRecordV1:
    """Replace the whole project document, steps included."""

    record = _call(
        lambda: get_store().update_project(
            project_id, request.model_dump(mode="json", exclude_none=True)
        )
    )
    return ProjectRecordV1.model_validate(record)


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str) -> Response:
    _call(lambda: get_store().delete_project(project_id))
    return Response(status_code=204)


@router.get(
    "/projects/{project_id}/state", response_model=ProjectWorkflowStateV1
)
async def get_project_workflow_state(project_id: str) -> ProjectWorkflowStateV1:
    """Return the project's 03-page state; unset state is an empty document."""

    return _call(lambda: get_store().get_project_workflow_state(project_id))


@router.put(
    "/projects/{project_id}/state", response_model=ProjectWorkflowStateV1
)
async def put_project_workflow_state(
    project_id: str, request: ProjectWorkflowStateV1
) -> ProjectWorkflowStateV1:
    """Save the project's 03-page state; dangling references are rejected."""

    return _call(lambda: get_store().save_project_workflow_state(project_id, request))


@router.delete("/projects/{project_id}/state", status_code=204)
async def delete_project_workflow_state(project_id: str) -> Response:
    _call(lambda: get_store().delete_project_workflow_state(project_id))
    return Response(status_code=204)


@router.get(
    "/projects/{project_id}/reports/latest",
    response_model=ReportResponse | None,
    response_model_exclude_none=False,
)
async def get_latest_project_report(project_id: str) -> ReportResponse | None:
    """Return the newest stored report, or ``null`` when the project has none."""

    return _call(lambda: get_store().get_latest_report(project_id))


@router.get(
    "/projects/{project_id}/reports/{report_id}", response_model=ReportResponse
)
async def get_project_report(project_id: str, report_id: str) -> ReportResponse:
    return _call(lambda: get_store().get_report(project_id, report_id))


@router.delete(
    "/projects/{project_id}/reports/{report_id}", status_code=204
)
async def delete_project_report(project_id: str, report_id: str) -> Response:
    _call(lambda: get_store().delete_report(project_id, report_id))
    return Response(status_code=204)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@router.post("/layouts", response_model=LayoutV1, status_code=201)
async def register_layout(request: LayoutRegisterRequest) -> LayoutV1:
    def operation():
        path = storage.resolve_gds_asset(request.assetId)
        from ..gds import inspect_gds
        data = path.read_bytes()
        info = inspect_gds(data)
        structures = []
        for structure in info["structures"]:
            try:
                bounds = structure_bounds(data, structure["name"])
            except Exception:
                bounds = None
            structures.append({**structure, "bounds": bounds})
        layout_id = f"layout_{request.assetId[:16]}"
        document = LayoutV1(
            schemaVersion=1, layoutId=layout_id, assetId=request.assetId,
            fileName=request.fileName, sizeBytes=info["fileSizeBytes"],
            units=info["units"], structures=structures, createdAt=_now_iso(),
        ).model_dump(mode="json", exclude_none=True)
        get_store().register_layout(layout_id, request.assetId, request.fileName, document)
        return LayoutV1.model_validate(document)
    return _call(operation)


@router.get("/layouts", response_model=LayoutListResponse)
async def list_layouts() -> LayoutListResponse:
    rows = _call(get_store().list_layouts)
    return LayoutListResponse(layouts=[LayoutSummaryV1.model_validate(row) for row in rows])


@router.get("/layouts/{layout_id}", response_model=LayoutV1)
async def get_layout(layout_id: str) -> LayoutV1:
    return LayoutV1.model_validate(_call(lambda: get_store().get_layout(layout_id)))


@router.post("/layouts/{layout_id}/preview", response_model=ClipPreviewResponse)
async def preview_layout(layout_id: str, request: ClipPreviewRequest) -> ClipPreviewResponse:
    def operation():
        layout = get_store().get_layout(layout_id)
        data = storage.resolve_gds_asset(layout["assetId"]).read_bytes()
        extracted = extract_clip_polygons(
            data,
            structure=request.structure,
            layers=[item.model_dump(mode="json") for item in request.layers],
            bbox=request.bbox.model_dump(mode="json"),
        )
        return ClipPreviewResponse.model_validate(extracted)
    return _call(operation)


@router.post("/clips", response_model=ClipV1, status_code=201)
async def create_clip(request: ClipCreateRequest) -> ClipV1:
    def operation():
        layout = get_store().get_layout(request.layoutId)
        data = storage.resolve_gds_asset(layout["assetId"]).read_bytes()
        # 2D clips stay byte-for-byte identical to the pre-3D documents: no
        # `baseHeight`/`height` keys at all, so their clipHash is unchanged.
        layer_payload = [item.model_dump(mode="json", exclude_none=True) for item in request.layers]
        extracted = extract_clip_polygons(
            data, structure=request.structure, layers=layer_payload,
            bbox=request.bbox.model_dump(mode="json"),
        )
        clip_id = f"clip_{uuid.uuid4().hex[:16]}"
        gds = build_clip_gds(extracted["polygons"], extracted["width"], extracted["height"])
        gds_asset_id = save_asset(gds)
        annotations = [
            {
                "annotationId": item.annotationId or f"ann_{uuid.uuid4().hex[:12]}",
                "text": item.text, "x": item.x, "y": item.y,
            }
            for item in request.annotations
        ]
        document = {
            "schemaVersion": 1, "clipId": clip_id, "name": request.name,
            "layoutId": request.layoutId, "assetId": layout["assetId"],
            "structure": request.structure, "layers": layer_payload,
            "bbox": request.bbox.model_dump(mode="json"), "annotations": annotations,
            "polygons": extracted["polygons"], "polygonCount": extracted["polygonCount"],
            "pointCount": extracted["pointCount"], "contentBounds": extracted["contentBounds"],
            "width": extracted["width"], "height": extracted["height"],
            "gdsAssetId": gds_asset_id, "gdsFileName": f"{clip_id}.gds",
            "createdAt": _now_iso(),
        }
        document["clipHash"] = canonical_clip_hash(document)
        get_store().save_clip(document)
        return ClipV1.model_validate(document)
    return _call(operation)


@router.get("/clips", response_model=ClipListResponse)
async def list_clips(layout_id: str | None = None) -> ClipListResponse:
    rows = _call(lambda: get_store().list_clips(layout_id))
    return ClipListResponse(clips=[ClipV1.model_validate(row) for row in rows])


@router.get("/clips/{clip_id}", response_model=ClipV1)
async def get_clip(clip_id: str) -> ClipV1:
    return ClipV1.model_validate(_call(lambda: get_store().get_clip(clip_id)))


@router.post("/clips/{clip_id}/annotations", response_model=ClipV1, status_code=201)
async def add_clip_annotation(clip_id: str, request: ClipAnnotationRequest) -> ClipV1:
    def operation():
        document = get_store().get_clip(clip_id)
        document["annotations"].append({
            "annotationId": f"ann_{uuid.uuid4().hex[:12]}",
            "text": request.text, "x": request.x, "y": request.y,
        })
        document["clipHash"] = canonical_clip_hash(document)
        get_store().save_clip(document)
        return ClipV1.model_validate(document)
    return _call(operation)


@router.post("/clips/{clip_id}/compile", response_model=AdoptProjectResponse, status_code=201)
async def compile_clip(clip_id: str, request: ClipCompileRequest) -> AdoptProjectResponse:
    def operation():
        clip = get_store().get_clip(clip_id)
        dimension = request.project.dimension
        grid = request.gridDelta or request.project.domain.gridDelta
        if dimension == 2:
            # 2D behaviour is frozen: layer→material only, no z placement.
            geometry_layers = [
                {"layer": item["layer"], "material": item["material"]}
                for item in clip["layers"]
            ]
            domain = {
                "gridDelta": grid,
                "xExtent": max(float(clip["width"]) + 2 * grid, 4 * grid),
                "xBoundary": "reflective",
            }
        else:
            geometry_layers = []
            for index, item in enumerate(clip["layers"]):
                label = f"第 {index + 1} 层（GDS layer {item['layer']}）"
                base_height = item.get("baseHeight")
                height = item.get("height")
                if base_height is None or height is None:
                    raise HTTPException(
                        status_code=422,
                        detail=(f"3D Clip {label}缺少 baseHeight/height：请在 01 的 GDS/Layout "
                                f"页签为每层填写基底高度与厚度后重新保存 Clip"),
                    )
                base_height = float(base_height)
                height = float(height)
                if not math.isfinite(base_height) or not math.isfinite(height):
                    raise HTTPException(
                        status_code=422,
                        detail=f"3D Clip {label}的 baseHeight/height 必须是有限数值",
                    )
                minimum = 2 * grid
                if height < minimum:
                    raise HTTPException(
                        status_code=422,
                        detail=(f"3D Clip {label}的厚度 {height:g} µm 小于两个网格步长 "
                                f"（{minimum:g} µm，gridDelta={grid:g}）：请增大厚度或减小 gridDelta"),
                    )
                geometry_layers.append({
                    "layer": item["layer"], "material": item["material"],
                    "baseHeight": base_height, "height": height,
                })
            domain = {
                "gridDelta": grid,
                "xExtent": max(float(clip["width"]) + 2 * grid, 4 * grid),
                "yExtent": max(float(clip["height"]) + 2 * grid, 4 * grid),
                "boundaryXY": "reflective",
            }
        payload = request.project.model_dump(mode="json", exclude_none=True)
        payload["geometry"] = {
            "kind": "gds", "assetId": clip["gdsAssetId"], "fileName": clip["gdsFileName"],
            "structure": "CLIP", "unitScale": 1, "position": [0, 0, 0],
            "layers": geometry_layers,
        }
        payload["domain"] = domain
        source = {
            "kind": "layout", "layoutId": clip["layoutId"], "clipId": clip["clipId"],
            "clipHash": clip["clipHash"], "structure": clip["structure"], "bbox": clip["bbox"],
            "polygonCount": clip["polygonCount"], "pointCount": clip["pointCount"],
            "annotations": clip["annotations"],
        }
        try:
            flow, binding, compiled = adopt_project(
                payload, flow_id=request.flowId, geometry_source=source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        get_store().register_binding(binding)
        get_store().register_compile(compiled)
        return AdoptProjectResponse(
            flowId=flow.flowId, bindingId=binding.bindingId,
            compileId=compiled.compileRecord.compileId, projectName=compiled.project.name,
            dimension=compiled.project.dimension, stepCount=len(compiled.project.steps),
            authoredSteps=flow.steps, binding=binding, compile=compiled,
        )
    return _call(operation)


@router.post("/templates", response_model=FlowTemplateV1, status_code=201)
async def register_template(template: FlowTemplateV1) -> FlowTemplateV1:
    return _call(lambda: get_store().register_template(template))


def _expand_definition(definition_id: str, variables: dict[str, float | int | str | bool]):
    store = get_store()
    definition = store.get_flow_definition(definition_id)
    try:
        expanded = expand_flow_definition(
            definition, store.module_history(), variables=variables
        )
    except ValueError as exc:
        raise WorkflowValidation(str(exc)) from exc
    return definition, expanded


@router.post("/modules", response_model=ModuleV1, status_code=201)
async def save_module(module: ModuleV1) -> ModuleV1:
    def operation():
        get_store().save_module(module)
        return module
    return _call(operation)


@router.get("/modules", response_model=ModuleListResponse)
async def list_modules() -> ModuleListResponse:
    rows = _call(get_store().list_modules)
    return ModuleListResponse(modules=[ModuleSummaryV1.model_validate(row) for row in rows])


@router.get("/modules/{module_id}", response_model=ModuleV1)
async def get_module(module_id: str) -> ModuleV1:
    return _call(lambda: get_store().get_module(module_id))


@router.post("/flows/definitions", response_model=FlowDefinitionV2, status_code=201)
async def save_flow_definition(definition: FlowDefinitionV2) -> FlowDefinitionV2:
    def operation():
        get_store().save_flow_definition(definition)
        return definition
    return _call(operation)


@router.get("/flows/definitions", response_model=FlowDefinitionListResponse)
async def list_flow_definitions() -> FlowDefinitionListResponse:
    rows = _call(get_store().list_flow_definitions)
    return FlowDefinitionListResponse(
        definitions=[FlowDefinitionSummaryV1.model_validate(row) for row in rows]
    )


@router.get("/flows/definitions/{definition_id}", response_model=FlowDefinitionV2)
async def get_flow_definition(definition_id: str) -> FlowDefinitionV2:
    return _call(lambda: get_store().get_flow_definition(definition_id))


@router.post("/library/export", response_model=LibraryBundleV1, response_model_exclude_none=True, status_code=201)
async def export_library(request: LibraryExportRequestV1) -> LibraryBundleV1:
    def operation():
        store = get_store()
        bundle = build_bundle(store, request)
        # Reuse the stored copy so a repeated export reports the original
        # createdAt instead of pretending the same content is a new bundle.
        return LibraryBundleV1.model_validate(store.save_library_bundle(bundle))
    return _call(operation)


@router.get("/library/bundles", response_model=LibraryBundleListResponse)
async def list_library_bundles() -> LibraryBundleListResponse:
    rows = _call(get_store().list_library_bundles)
    return LibraryBundleListResponse(
        bundles=[LibraryBundleSummaryV1.model_validate(row) for row in rows]
    )


@router.get("/library/bundles/{bundle_id}", response_model=LibraryBundleV1, response_model_exclude_none=True)
async def get_library_bundle(bundle_id: str) -> LibraryBundleV1:
    return _call(lambda: LibraryBundleV1.model_validate(get_store().get_library_bundle(bundle_id)))


@router.post("/library/bundles/{bundle_id}/import", response_model=LibraryImportResultV1)
async def import_library_bundle(
    bundle_id: str, request: LibraryBundleImportRequest
) -> LibraryImportResultV1:
    def operation():
        store = get_store()
        bundle = LibraryBundleV1.model_validate(store.get_library_bundle(bundle_id))
        return import_bundle(store, bundle, request.policy)
    return _call(operation)


@router.post("/library/import", response_model=LibraryImportResultV1)
async def import_library_document(request: LibraryDirectImportRequest) -> LibraryImportResultV1:
    def operation():
        store = get_store()
        # Import first: a rejected bundle must not leave a catalog entry behind.
        result = import_bundle(store, request.bundle, request.policy)
        store.save_library_bundle(request.bundle)
        return result
    return _call(operation)


@router.get("/library/items", response_model=LibraryItemListResponse)
async def list_library_items(category: LibraryCategory | None = None) -> LibraryItemListResponse:
    rows = _call(lambda: get_store().list_library_items(category))
    return LibraryItemListResponse(
        items=[LibraryItemV1.model_validate(row) for row in rows]
    )


@router.post("/library/items", response_model=LibraryItemV1, status_code=201)
async def post_library_item(request: LibraryItemCreateRequest) -> LibraryItemV1:
    def operation():
        item = create_library_item(
            category=request.category,
            name=request.name,
            description=request.description,
            payload=request.payload,
        )
        return get_store().save_library_item(
            item.model_dump(mode="json"),
            max_per_category=MAX_LIBRARY_ITEMS_PER_CATEGORY,
        )

    return LibraryItemV1.model_validate(_call(operation))


@router.get("/library/items/{item_id}", response_model=LibraryItemV1)
async def get_library_item(item_id: str) -> LibraryItemV1:
    return LibraryItemV1.model_validate(_call(lambda: get_store().get_library_item(item_id)))


@router.put("/library/items/{item_id}", response_model=LibraryItemV1)
async def put_library_item(item_id: str, request: LibraryItemUpdateRequest) -> LibraryItemV1:
    def operation():
        store = get_store()
        existing = LibraryItemV1.model_validate(store.get_library_item(item_id))
        updated = update_library_item(
            existing,
            name=request.name,
            description=request.description,
            payload=request.payload,
        )
        return store.update_library_item(updated.model_dump(mode="json"))

    return LibraryItemV1.model_validate(_call(operation))


@router.delete("/library/items/{item_id}", status_code=204)
async def delete_library_item(item_id: str) -> Response:
    _call(lambda: get_store().delete_library_item(item_id))
    return Response(status_code=204)


@router.post("/flows/definitions/{definition_id}/expand", response_model=ExpandedFlowV1)
async def expand_definition(
    definition_id: str, request: ExpandFlowDefinitionRequest
) -> ExpandedFlowV1:
    _, expanded = _call(lambda: _expand_definition(definition_id, request.variables))
    return expanded


@router.post(
    "/flows/definitions/{definition_id}/compile",
    response_model=AdoptProjectResponse,
    status_code=201,
)
async def compile_definition(
    definition_id: str, request: CompileFlowDefinitionRequest
) -> AdoptProjectResponse:
    def operation():
        store = get_store()
        definition, expanded = _expand_definition(definition_id, request.variables)
        if request.flowId is not None and request.flowId != definition.flowId:
            raise WorkflowValidation(
                f"flowId {request.flowId!r} does not match the definition flowId "
                f"{definition.flowId!r}; stable step ids depend on it"
            )
        try:
            flow, placements = to_flow_revision(definition, expanded)
        except ValueError as exc:
            raise WorkflowValidation(str(exc)) from exc
        payload = request.project.model_dump(mode="json", exclude_none=True)
        payload["steps"] = [
            step.model_dump(mode="json", exclude_none=True) for step in flow.steps
        ]
        # The definition owns the execution solver; the project only supplies
        # geometry/binding context.
        payload["solver"] = flow.solver.model_dump(mode="json", exclude_none=True)
        variable_set = [
            variable.model_dump(mode="json", exclude_none=True)
            for variable in expanded.variables
        ]
        overrides = [
            {"name": name, "value": value, "source": "request"}
            for name, value in sorted(request.variables.items())
        ]
        module_revisions = [
            value.model_dump(mode="json", exclude_none=True)
            for value in expanded.moduleRevisions
        ]
        flow_paths = {step.authoredStepId: step.flowPath for step in expanded.steps}
        parameter_provenance = {
            step.authoredStepId: step.parameterProvenance for step in expanded.steps
        }
        step_variable_refs = {
            step.authoredStepId: step.variableRefs for step in expanded.steps
        }
        try:
            adopted_flow, binding, compiled = adopt_project(
                payload,
                flow_id=definition.flowId,
                steps=flow.steps,
                revision=definition.revision,
                placements=placements,
                flow_paths=flow_paths,
                parameter_provenance=parameter_provenance,
                variable_refs=step_variable_refs,
                variable_set=variable_set,
                overrides=overrides,
                module_revisions=module_revisions,
            )
        except ValueError as exc:
            raise WorkflowValidation(str(exc)) from exc
        store.register_binding(binding)
        store.register_compile(compiled)
        return AdoptProjectResponse(
            flowId=adopted_flow.flowId,
            bindingId=binding.bindingId,
            compileId=compiled.compileRecord.compileId,
            projectName=compiled.project.name,
            dimension=compiled.project.dimension,
            stepCount=len(compiled.project.steps),
            authoredSteps=flow.steps,
            binding=binding,
            compile=compiled,
        )
    return _call(operation)


@router.get("/templates", response_model=TemplateListResponse)
async def list_templates() -> TemplateListResponse:
    rows = _call(get_store().list_templates)
    return TemplateListResponse(templates=[TemplateSummaryV1.model_validate(row) for row in rows])


@router.get("/templates/{template_id}", response_model=FlowTemplateV1)
async def get_template(template_id: str) -> FlowTemplateV1:
    return _call(lambda: get_store().get_template(template_id))


@router.post("/flows/from-template", response_model=AdoptProjectResponse, status_code=201)
async def adopt_template(request: FromTemplateRequest) -> AdoptProjectResponse:
    def operation():
        template = get_store().get_template(request.templateId)
        template_steps = resolve_template(template, request.variables)
        if request.mode == "append":
            nonce = uuid.uuid4().hex[:8]
            inserted = [
                step.model_copy(update={"id": f"tpl_{nonce}_{index:02d}"})
                for index, step in enumerate(template_steps, start=1)
            ]
            steps = [*request.project.steps, *inserted]
        else:
            steps = template_steps
        flow, binding, compiled = adopt_project(
            request.project.model_dump(mode="json", exclude_none=True),
            flow_id=request.flowId,
            steps=steps,
        )
        get_store().register_binding(binding)
        get_store().register_compile(compiled)
        return AdoptProjectResponse(
            flowId=flow.flowId, bindingId=binding.bindingId,
            compileId=compiled.compileRecord.compileId, projectName=compiled.project.name,
            dimension=compiled.project.dimension, stepCount=len(compiled.project.steps),
            authoredSteps=steps, binding=binding, compile=compiled,
        )
    return _call(operation)


@router.get("/compiles/{left_id}/diff/{right_id}", response_model=CompileDiffV1)
async def compile_diff(left_id: str, right_id: str) -> CompileDiffV1:
    def operation():
        store = get_store()
        left = store.get_compile(left_id)
        right = store.get_compile(right_id)
        left_steps = {step.id: step.model_dump(mode="json", exclude_none=True) for step in left.project.steps}
        right_steps = {step.id: step.model_dump(mode="json", exclude_none=True) for step in right.project.steps}
        added = sorted(set(right_steps) - set(left_steps))
        removed = sorted(set(left_steps) - set(right_steps))
        changed = sorted(key for key in set(left_steps) & set(right_steps) if left_steps[key] != right_steps[key])
        left_order = [step.id for step in left.project.steps]
        right_order = [step.id for step in right.project.steps]
        common = set(left_order) & set(right_order)
        moved = [step_id for step_id in right_order if step_id in common and left_order.index(step_id) != right_order.index(step_id)]
        affected = sorted(set(changed) | set(moved) | set(added))
        return CompileDiffV1(
            addedStepIds=added, removedStepIds=removed, movedStepIds=moved,
            changedStepIds=changed, affectedStepIds=affected, changedVariables=[],
            details=[{"stepId": key, "before": left_steps[key], "after": right_steps[key]} for key in changed],
        )
    return _call(operation)


@router.post("/routes/{route_id}/branches", response_model=RoutePlanResponse, status_code=201)
async def branch_route(route_id: str, request: BranchRequest) -> RoutePlanResponse:
    plan = _call(lambda: create_branch(
        get_store(), route_id, request.compileId, request.startAfterStepId, request.changedStepIds,
    ))
    return RoutePlanResponse(plan=plan)


@router.post(
    "/routes/{route_id}/measurement-decisions",
    response_model=MeasurementDecisionResponse,
    status_code=201,
)
async def create_measurement_decision_endpoint(
    route_id: str, request: MeasurementDecisionRequestV1
) -> MeasurementDecisionResponse:
    result = _call(lambda: create_measurement_decision(get_store(), route_id, request))
    return MeasurementDecisionResponse.model_validate(result)


@router.get(
    "/routes/{route_id}/measurement-decisions",
    response_model=MeasurementDecisionListResponse,
)
async def list_measurement_decisions_endpoint(
    route_id: str,
) -> MeasurementDecisionListResponse:
    def operation():
        get_store().get_plan(route_id)
        return get_store().list_measurement_decisions(route_id)

    return MeasurementDecisionListResponse(decisions=_call(operation))


@router.get(
    "/routes/{route_id}/measurement-decisions/{decision_id}",
    response_model=MeasurementDecisionV1,
)
async def get_measurement_decision_endpoint(
    route_id: str, decision_id: str
) -> MeasurementDecisionV1:
    return _call(
        lambda: get_store().get_measurement_decision(decision_id, route_id=route_id)
    )


@router.post(
    "/routes/{route_id}/measurement-decisions/{decision_id}/replay",
    response_model=MeasurementDecisionResponse,
    status_code=201,
)
async def replay_measurement_decision_endpoint(
    route_id: str, decision_id: str
) -> MeasurementDecisionResponse:
    result = _call(
        lambda: replay_measurement_decision(get_store(), route_id, decision_id)
    )
    return MeasurementDecisionResponse.model_validate(result)


@router.get(
    "/routes/{route_id}/workflow-runs",
    response_model=WorkflowRunListResponse,
)
async def list_workflow_runs_endpoint(route_id: str) -> WorkflowRunListResponse:
    def operation():
        get_store().get_plan(route_id)
        return list_workflow_runs(get_store(), route_id)

    return WorkflowRunListResponse(runs=_call(operation))


@router.get(
    "/routes/{route_id}/workflow-runs/{workflow_run_id}",
    response_model=WorkflowRunDetailV1,
)
async def get_workflow_run_endpoint(
    route_id: str, workflow_run_id: str
) -> WorkflowRunDetailV1:
    return _call(lambda: get_workflow_run(get_store(), route_id, workflow_run_id))


@router.post("/routes", response_model=RoutePlanResponse, status_code=201)
async def create_route(plan: RoutePlanV1) -> RoutePlanResponse:
    stored = _call(lambda: get_store().create_plan(plan))
    return RoutePlanResponse(plan=stored)


@router.get("/routes", response_model=RouteListResponse)
async def list_routes() -> RouteListResponse:
    rows = _call(get_store().list_plans)
    return RouteListResponse(routes=[RouteSummaryV1.model_validate(row) for row in rows])


@router.get("/routes/{route_id}", response_model=RoutePlanResponse)
async def get_route(route_id: str) -> RoutePlanResponse:
    plan = _call(lambda: get_store().get_plan(route_id))
    return RoutePlanResponse(plan=plan)


@router.post("/routes/{route_id}/attempts", response_model=RoutePlanResponse)
async def append_attempt(route_id: str, request: AppendAttemptRequest) -> RoutePlanResponse:
    plan = _call(
        lambda: get_store().append_attempt(
            route_id,
            request.attempt,
            request.segments,
            request.reuse,
        )
    )
    return RoutePlanResponse(plan=plan)


@router.get("/routes/{route_id}/attempts/{attempt_id}/materialize", response_model=MaterializeResponse)
async def materialize_route(route_id: str, attempt_id: str) -> MaterializeResponse:
    segments = _call(lambda: get_store().materialize(route_id, attempt_id))
    return MaterializeResponse(attemptId=attempt_id, segments=segments)


@router.get("/attempts/{attempt_id}", response_model=AttemptDetailResponse)
async def get_attempt(attempt_id: str) -> AttemptDetailResponse:
    detail = _call(lambda: get_store().get_attempt_detail(attempt_id))
    return AttemptDetailResponse.model_validate(detail)


@router.post("/attempts/{attempt_id}/cancel", response_model=AttemptV1)
async def cancel_attempt(attempt_id: str) -> AttemptV1:
    return _call(lambda: get_store().update_attempt_status(attempt_id, "cancelled"))


@router.get("/segments/{segment_id}", response_model=SegmentV1)
async def get_segment(segment_id: str) -> SegmentV1:
    return _call(lambda: get_store().get_segment(segment_id))


@router.post("/segments/{segment_id}/execute", response_model=SegmentExecutionResponse)
async def execute_segment_endpoint(segment_id: str, request: Request) -> SegmentExecutionResponse:
    supervisor = getattr(request.app.state, "supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail="supervisor is unavailable")
    segment, run_id = _call(lambda: execute_segment(get_store(), supervisor, segment_id))
    return SegmentExecutionResponse(segment=segment, runId=run_id)


@router.post("/segments/{segment_id}/cancel", response_model=SegmentV1)
async def cancel_segment(segment_id: str, request: Request) -> SegmentV1:
    store = get_store()
    segment = _call(lambda: store.get_segment(segment_id))
    if segment.runId and segment.status == "running":
        supervisor = getattr(request.app.state, "supervisor", None)
        if supervisor is not None:
            supervisor.cancel(segment.runId)
            return segment
    return _call(lambda: store.update_segment_status(segment_id, "cancelled"))


@router.get("/execution-catalog", response_model=dict)
def execution_catalog_endpoint() -> dict:
    """Return the locked process/field/geometry catalog for UI consumers."""
    from process_models.execution import load_execution_registry
    from process_models.v2.catalog import execution_catalog

    return execution_catalog(load_execution_registry())
