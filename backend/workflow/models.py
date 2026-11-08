"""Workflow-layer contracts around the frozen ProjectV6/V7 execution schema."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from ..materials import MaterialDefinition
from ..models import (
    Domain,
    Domain3D,
    ProjectV7,
    SolverV7,
    StepV7,
    StrictModel,
    Units,
)
from .hashing import compile_record_id, full_project_hash, source_map_hash


class ClipGeometrySourceV1(StrictModel):
    """Frozen provenance for a layout clip used to build one binding."""

    kind: Literal["layout"] = "layout"
    layoutId: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    clipId: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    clipHash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    structure: Annotated[str, Field(min_length=1, max_length=512)]
    bbox: dict[str, float]
    polygonCount: Annotated[int, Field(ge=1)]
    pointCount: Annotated[int, Field(ge=3)]
    annotations: list[dict[str, Any]] = Field(default_factory=list)


class GeometryBindingV1(StrictModel):
    """An immutable application of a geometry source to one compile.

    ``Clip`` stays editable. This object freezes the material mapping, stack,
    domain, grid and coordinate transform that the compiler actually consumed.
    """

    schemaVersion: Literal[1]
    bindingId: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    dimension: Literal[2, 3]
    units: Units
    domain: Domain | Domain3D
    geometry: dict[str, Any]
    geometrySource: ClipGeometrySourceV1 | None = None
    materialDefinitions: Annotated[list[MaterialDefinition], Field(max_length=64)] | None = None
    materialCatalogVersion: Annotated[str, Field(min_length=1, max_length=32)] = "4.6.2"
    defaultValuesVersion: Annotated[str, Field(min_length=1, max_length=32)] = "1"
    schemaVersions: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def domain_matches_dimension(self) -> "GeometryBindingV1":
        if self.dimension == 2 and not isinstance(self.domain, Domain):
            raise ValueError("2D GeometryBinding requires a 2D domain")
        if self.dimension == 3 and not isinstance(self.domain, Domain3D):
            raise ValueError("3D GeometryBinding requires a 3D domain")
        return self


class ParameterProvenanceV1(StrictModel):
    kind: Literal["literal", "variable", "expression"]
    source: str | None = None
    resolvedValue: Any = None


class SourceMapEntryV1(StrictModel):
    stepId: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    flowPath: Annotated[str, Field(min_length=1, max_length=512)]
    authoredStepId: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    moduleInstanceIdPath: Annotated[str, Field(min_length=1, max_length=256)]
    iterationKey: Annotated[str, Field(min_length=1, max_length=32, pattern=r"^i[0-9]+$")]
    parameterProvenance: dict[str, ParameterProvenanceV1] = Field(default_factory=dict)
    variableRefs: list[str] = Field(default_factory=list)


class FlowRevisionV1(StrictModel):
    """P0 flow definition: literal steps, fixed solver defaults, no geometry."""

    schemaVersion: Literal[1]
    flowId: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    revision: Annotated[int, Field(ge=1)]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    steps: Annotated[list[StepV7], Field(min_length=1)]
    solver: SolverV7

    @model_validator(mode="after")
    def unique_authored_step_ids(self) -> "FlowRevisionV1":
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("authored step IDs must be unique")
        if self.solver.random.mode != "fixed":
            raise ValueError("P0 FlowRevision requires a fixed random seed")
        return self


class ModuleRevisionRefV1(StrictModel):
    """One concrete module revision used by an expansion or compile."""

    moduleId: Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
    revision: Annotated[int, Field(ge=1)]


class CompileRecordV1(StrictModel):
    compileId: Annotated[str, Field(pattern=r"^c_[a-f0-9]{24}$")]
    compilerVersion: Annotated[str, Field(min_length=1, max_length=32)]
    flowId: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    flowRevisionId: Annotated[str, Field(min_length=3, max_length=96)]
    flowRevisionHash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    geometryBindingId: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    geometryBindingHash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    variableSet: list[dict[str, Any]] = Field(default_factory=list)
    overrides: list[dict[str, Any]] = Field(default_factory=list)
    moduleRevisions: list[ModuleRevisionRefV1] | None = None
    materialCatalogVersion: Annotated[str, Field(min_length=1, max_length=32)]
    materialDefinitionsHash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    defaultValuesVersion: Annotated[str, Field(min_length=1, max_length=32)]
    engineVersions: dict[str, str] = Field(default_factory=dict)
    seedPolicy: Literal["shared-run-seed"] = "shared-run-seed"
    stepIdPolicy: Annotated[str, Field(min_length=1, max_length=64)]
    fullProjectHash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    sourceMapHash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    createdAt: str | None = None


class CompiledWorkflowV1(StrictModel):
    """Persisted/compiled envelope with content-addressed consistency checks."""

    project: ProjectV7
    sourceMap: list[SourceMapEntryV1]
    compileRecord: CompileRecordV1

    @model_validator(mode="after")
    def content_matches_record(self) -> "CompiledWorkflowV1":
        expected_project = full_project_hash(self.project)
        if expected_project != self.compileRecord.fullProjectHash:
            raise ValueError("compiled project content does not match fullProjectHash")
        expected_source_map = source_map_hash(self.sourceMap)
        if expected_source_map != self.compileRecord.sourceMapHash:
            raise ValueError("SourceMap content does not match sourceMapHash")
        project_step_ids = [step.id for step in self.project.steps]
        source_step_ids = [entry.stepId for entry in self.sourceMap]
        if project_step_ids != source_step_ids:
            raise ValueError("SourceMap step ids must match compiled project step order")
        expected_compile_id = compile_record_id(
            compiler_version=self.compileRecord.compilerVersion,
            flow_revision_hash=self.compileRecord.flowRevisionHash,
            geometry_binding_hash=self.compileRecord.geometryBindingHash,
            material_definitions_hash=self.compileRecord.materialDefinitionsHash,
            variable_set=self.compileRecord.variableSet,
            overrides=self.compileRecord.overrides,
            seed_policy=self.compileRecord.seedPolicy,
            step_id_policy=self.compileRecord.stepIdPolicy,
            engine_versions=self.compileRecord.engineVersions,
            module_revisions=self.compileRecord.moduleRevisions,
        )
        if expected_compile_id != self.compileRecord.compileId:
            raise ValueError("compile record fields do not match compileId")
        return self

    def verify(self) -> "CompiledWorkflowV1":
        """Re-check a live envelope before persistence or execution submission."""

        return CompiledWorkflowV1.model_validate(self.model_dump(mode="json", exclude_none=True))


class CompileDiffV1(StrictModel):
    addedStepIds: list[str] = Field(default_factory=list)
    removedStepIds: list[str] = Field(default_factory=list)
    movedStepIds: list[str] = Field(default_factory=list)
    changedStepIds: list[str] = Field(default_factory=list)
    changedVariables: list[str] = Field(default_factory=list)
    affectedStepIds: list[str] = Field(default_factory=list)
    details: list[dict] = Field(default_factory=list)
    suggestedBaseCheckpoint: str | None = None
