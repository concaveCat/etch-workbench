"""Strict version-one request models shared by API and storage."""

from __future__ import annotations

import math
import re
import hashlib
import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator, model_validator
from process_models.v2.options import AdvectionOverride
from .materials import MaterialDefinition
from .implant_models import reserved_label_error
from .runtime_limits import MAX_CELL_SET_CELLS, available_cpu_threads


def _strict_message(path: str | None, message: str) -> str:
    return f"{path}: {message}" if path else message


def strict_finite_number(value: object, path: str | None = None) -> float:
    """Reject bool/string/NaN/Inf before any native numeric conversion.

    Shared by the Pydantic request models and the direct worker path so a
    hand-written worker config cannot reach a native constructor with values
    the API would reject.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(_strict_message(path, "must be a JSON number"))
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(_strict_message(path, "must be finite")) from None
    if not math.isfinite(result):
        raise ValueError(_strict_message(path, "must be finite"))
    return result


def strict_integer(value: object, path: str | None = None) -> int:
    if isinstance(value, bool) or type(value) is not int:
        raise ValueError(_strict_message(path, "must be a JSON integer"))
    return value


def strict_boolean(value: object, path: str | None = None) -> bool:
    if type(value) is not bool:
        raise ValueError(_strict_message(path, "must be a JSON boolean"))
    return value


def _finite_number(value: object) -> float:
    return strict_finite_number(value)


def _integer(value: object) -> int:
    return strict_integer(value)


Number = Annotated[float, BeforeValidator(_finite_number)]
Integer = Annotated[int, BeforeValidator(_integer)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class _UnitsBase(StrictModel):
    time: Literal["s"]
    angle: Literal["deg"]


class Units(_UnitsBase):
    """Schema-7 native units; nanometres and micrometres are both first-class."""

    length: Literal["um", "nm"]
    rate: Literal["um/s", "nm/s"]

    @model_validator(mode="after")
    def paired_length_and_rate(self) -> "Units":
        if (self.length == "nm") != (self.rate == "nm/s"):
            raise ValueError("units.length and units.rate must use the same length unit")
        return self

    @property
    def length_units_per_um(self) -> float:
        """Return native length units per micrometre (1 for um, 1000 for nm)."""
        return 1000.0 if self.length == "nm" else 1.0

    @property
    def length_unit(self) -> str:
        return self.length


class UnitsLegacy(_UnitsBase):
    """Schemas 1-6 are frozen in the historical micrometre frame."""

    length: Literal["um"]
    rate: Literal["um/s"]


# Minimum lateral 3D domain size in the declared length unit. A domain must
# hold real structure (a 22 nm FinFET cell is 0.48 x 0.50 um), so the
# historical 1 um floor was an arbitrary UI limit, not a native requirement.
# The FinFET acceptance example uses a 90 x 100 nm cell in a nanometre project.
# 0.01 still rejects a degenerate or zero-sized domain, and ``Domain3D``
# additionally requires two grid intervals per axis.
MIN_DOMAIN_EXTENT_3D = 0.01


class Domain(StrictModel):
    gridDelta: Annotated[Number, Field(gt=0, le=500)]
    xExtent: Annotated[Number, Field(ge=1, le=40000)]
    xBoundary: Literal["reflective", "periodic"]


class PlaneGeometry(StrictModel):
    kind: Literal["plane"]
    height: Number
    substrateMaterial: Literal["Si"]


class TrenchGeometry(StrictModel):
    kind: Literal["trench"]
    width: Annotated[Number, Field(gt=0)]
    depth: Annotated[Number, Field(gt=0)]
    taperAngle: Annotated[Number, Field(ge=-45, le=45)]
    maskHeight: Annotated[Number, Field(ge=0)]
    maskTaperAngle: Annotated[Number, Field(ge=-45, le=45)]
    substrateMaterial: Literal["Si"]


Geometry = Annotated[Union[PlaneGeometry, TrenchGeometry], Field(discriminator="kind")]


class Step(StrictModel):
    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    operation: Literal["etch", "deposit"]
    model: Literal["isotropic", "singleParticle"]
    rateMagnitude: Annotated[Number, Field(ge=0, le=2)]
    duration: Annotated[Number, Field(ge=0.01, le=600)]
    depositMaterial: Literal["SiO2"] | None = None
    stickingProbability: Annotated[Number, Field(ge=0, le=1)] | None = None
    sourceExponent: Annotated[Number, Field(ge=0, le=20)] | None = None

    @model_validator(mode="after")
    def check_model_fields(self) -> "Step":
        if self.operation == "deposit" and self.depositMaterial != "SiO2":
            raise ValueError("deposit steps require depositMaterial=SiO2")
        if self.operation == "etch" and "depositMaterial" in self.model_fields_set:
            raise ValueError("depositMaterial applies only to deposit steps")
        particle_fields = (self.stickingProbability, self.sourceExponent)
        if self.model == "singleParticle" and any(value is None for value in particle_fields):
            raise ValueError("singleParticle requires stickingProbability and sourceExponent")
        if self.model == "isotropic" and ({"stickingProbability", "sourceExponent"} & self.model_fields_set):
            raise ValueError("particle parameters apply only to singleParticle")
        return self


class FixedRandom(StrictModel):
    mode: Literal["fixed"]
    seed: Annotated[Integer, Field(ge=0, le=4294967295)]


class UnseededRandom(StrictModel):
    mode: Literal["random"]
    seed: None = None

    @model_validator(mode="after")
    def omit_seed(self) -> "UnseededRandom":
        if "seed" in self.model_fields_set:
            raise ValueError("random mode must omit seed; the server generates it when a particle step runs")
        return self


RandomSettings = Annotated[Union[FixedRandom, UnseededRandom], Field(discriminator="mode")]


class Solver(StrictModel):
    timeStepRatio: Annotated[Number, Field(ge=0.05, le=0.5)]
    raysPerPoint: Annotated[Integer, Field(ge=16, le=4096)]
    cpuThreads: Annotated[Integer, Field(ge=1)]
    random: RandomSettings
    enginePreference: Literal["auto", "cpu", "gpu"]
    timeoutSeconds: Annotated[Integer, Field(ge=0, le=86400)] = 0

    @field_validator("cpuThreads")
    @classmethod
    def cpu_threads_within_available_limit(cls, value: int) -> int:
        maximum = available_cpu_threads()
        if value > maximum:
            raise ValueError(f"must not exceed the {maximum} CPU threads available to this process")
        return value


class AdvectionSettingsV7(StrictModel):
    spatialScheme: Literal[
        "ENGQUIST_OSHER_1ST_ORDER", "ENGQUIST_OSHER_2ND_ORDER", "LAX_FRIEDRICHS_1ST_ORDER",
        "LAX_FRIEDRICHS_2ND_ORDER", "LOCAL_LAX_FRIEDRICHS_ANALYTICAL_1ST_ORDER",
        "LOCAL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER", "LOCAL_LOCAL_LAX_FRIEDRICHS_2ND_ORDER",
        "LOCAL_LAX_FRIEDRICHS_1ST_ORDER", "LOCAL_LAX_FRIEDRICHS_2ND_ORDER",
        "STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER", "WENO_3RD_ORDER", "WENO_5TH_ORDER",
    ] = "ENGQUIST_OSHER_1ST_ORDER"
    temporalScheme: Literal["FORWARD_EULER", "RUNGE_KUTTA_2ND_ORDER", "RUNGE_KUTTA_3RD_ORDER"] = "FORWARD_EULER"
    adaptiveTimeStepping: bool = False
    adaptiveTimeStepSubdivisions: Annotated[Integer, Field(ge=1, le=256)] = 20
    calculateIntermediateVelocities: bool = False
    checkDissipation: bool = True
    dissipationAlpha: Annotated[Number, Field(ge=0, le=10)] = 1.0
    ignoreVoids: bool = False
    velocityOutput: bool = False


class RayTracingSettingsV7(StrictModel):
    diskRadius: Annotated[Number, Field(ge=0, le=100)] = 0.0
    ignoreFluxBoundaries: bool = False
    maxBoundaryHits: Annotated[Integer, Field(ge=1, le=1000000)] = 1000
    maxReflections: Annotated[Integer, Field(ge=0, le=4294967295)] = 4294967295
    minNodeDistanceFactor: Annotated[Number, Field(ge=0, le=1)] = 0.05
    normalizationType: Literal["SOURCE", "MAX"] = "SOURCE"
    smoothingNeighbors: Annotated[Integer, Field(ge=0, le=1024)] = 1


class SolverV7(Solver):
    # Stable physical NVIDIA device UUID. Missing means host-preferred CUDA
    # device; older schema-1—6 projects remain unchanged and hash-compatible.
    gpuDeviceId: Annotated[str, Field(pattern=r"^GPU-[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")] | None = None
    # Concrete native flux engine for an explicit GPU request. Omitted keeps
    # the historical GPU_DISK route byte-for-byte (hash-compatible). The
    # installed GPU_TRIANGLE backend is only admitted for evidence-backed
    # combinations; an unproven request is rejected instead of falling back.
    gpuEngine: Literal["GPU_DISK", "GPU_TRIANGLE"] | None = None
    # Optional engine-plugin selection. Omitted keeps the native ViennaPS
    # policy byte-for-byte; set together with engineKernel to address a
    # custom engine declared by an installed engine.toml.
    engineId: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")] | None = None
    engineKernel: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    engineBackend: Literal["cpu", "gpu"] | None = None

    @model_validator(mode="after")
    def gpu_engine_requires_gpu_preference(self) -> "SolverV7":
        if self.gpuEngine is not None and self.enginePreference != "gpu":
            raise ValueError("gpuEngine requires solver.enginePreference=gpu; select the GPU engine explicitly or omit gpuEngine")
        if (self.engineId is None) != (self.engineKernel is None):
            raise ValueError("engineId and engineKernel must be provided together")
        if self.engineId is not None and self.gpuEngine is not None:
            raise ValueError("engineId is mutually exclusive with the native gpuEngine selection")
        if self.engineBackend is not None and self.engineId is None:
            raise ValueError("engineBackend requires engineId")
        return self
    advection: AdvectionSettingsV7 = Field(default_factory=AdvectionSettingsV7)
    rayTracing: RayTracingSettingsV7 = Field(default_factory=RayTracingSettingsV7)


class View(StrictModel):
    panX: Number
    panH: Number
    zoom: Annotated[Number, Field(gt=0, le=20)]
    showGrid: bool


class ProjectV1(StrictModel):
    schemaVersion: Literal[1]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    dimension: Literal[2]
    units: UnitsLegacy
    domain: Domain
    geometry: Geometry
    steps: list[Step]
    solver: Solver
    view: View

    @model_validator(mode="after")
    def unique_step_ids(self) -> "ProjectV1":
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step IDs must be unique")
        return self


class Domain3D(StrictModel):
    gridDelta: Annotated[Number, Field(gt=0, le=500)]
    xExtent: Annotated[Number, Field(ge=MIN_DOMAIN_EXTENT_3D, le=40000)]
    yExtent: Annotated[Number, Field(ge=MIN_DOMAIN_EXTENT_3D, le=40000)]
    boundaryXY: Literal["reflective", "periodic"]
    #: Optional absolute native bounds ``[xmin, xmax, ymin, ymax, zmin, zmax]``.
    #: The centered ``xExtent``/``yExtent`` constructor used by the UI cannot
    #: reproduce a native script whose bounds start at a non-centered origin:
    #: it always creates an odd number of grid points per axis. The FinFET
    #: acceptance fixture uses this field to reproduce the original
    #: ``ps.Domain(bounds, ...)`` grid exactly. Omitted keeps the historical
    #: centered domain byte-for-byte (and out of the request hash).
    bounds: Annotated[list[Number], Field(min_length=6, max_length=6)] | None = None

    @field_validator("xExtent", "yExtent")
    @classmethod
    def extent_spans_two_cells(cls, value: float, info: Any) -> float:
        delta = info.data.get("gridDelta")
        if delta is not None and float(value) < 2.0 * float(delta):
            raise ValueError(
                f"must span at least two grid intervals ({2.0 * float(delta):g} um for "
                f"gridDelta={float(delta):g})")
        return value

    @model_validator(mode="after")
    def lateral_extent_spans_two_cells(self) -> "Domain3D":
        # Native mesh construction needs at least two cells per axis; keeping
        # the absolute floor low is only safe together with this check.
        minimum = 2 * float(self.gridDelta)
        smallest = min(float(self.xExtent), float(self.yExtent))
        if smallest < minimum:
            raise ValueError(f"xExtent and yExtent must span at least two grid intervals "
                             f"({minimum:g} um for gridDelta={float(self.gridDelta):g})")
        if self.bounds is not None:
            xmin, xmax, ymin, ymax, zmin, zmax = (float(value) for value in self.bounds)
            if not xmax > xmin or not ymax > ymin or not zmax > zmin:
                raise ValueError("bounds must be ordered [xmin, xmax, ymin, ymax, zmin, zmax]")
            for label, span, extent in (
                ("x", xmax - xmin, float(self.xExtent)),
                ("y", ymax - ymin, float(self.yExtent)),
            ):
                if abs(span - extent) > 1e-9:
                    raise ValueError(f"bounds {label} span {span:g} must equal {label}Extent {extent:g}")
                if span < minimum:
                    raise ValueError(f"bounds {label} span must be at least two grid intervals ({minimum:g} um)")
                if span > 40000.0:
                    raise ValueError(f"bounds {label} span must stay within the application domain limit")
            if abs(zmin) / float(self.gridDelta) > 1_000_000 or abs(zmax) / float(self.gridDelta) > 1_000_000:
                raise ValueError("bounds z coordinates exceed the application coordinate budget")
        return self


def _select_dimension_domain(data: Any, model_class: type[BaseModel]) -> Any:
    """Validate ``domain`` against the branch selected by ``dimension``.

    ``domain`` is declared as ``Domain | Domain3D``. A plain union reports the
    errors of both branches, so a 3D payload also complains about missing 2D
    ``xBoundary``/``boundaryXY`` fields, which is noise for the caller. The
    branch is picked before validation from the declared dimension; the public
    JSON schema keeps the union and unchanged field names.
    """
    if isinstance(data, dict) and isinstance(data.get("domain"), dict):
        dimension = data.get("dimension")
        model = Domain3D if type(dimension) is int and dimension == 3 else (
            Domain if type(dimension) is int and dimension == 2 else None)
        if model is not None:
            try:
                return {**data, "domain": model.model_validate(data["domain"])}
            except ValidationError as exc:
                raise ValidationError.from_exception_data(
                    model_class.__name__,
                    [{**error, "loc": ("domain",) + tuple(error["loc"])} for error in exc.errors()],
                ) from None
    return data


class HoleGeometry(StrictModel):
    kind: Literal["hole"]
    radius: Annotated[Number, Field(gt=0)]
    depth: Annotated[Number, Field(gt=0)]
    taperAngle: Annotated[Number, Field(ge=-45, le=45)]
    maskHeight: Annotated[Number, Field(ge=0)]
    maskTaperAngle: Annotated[Number, Field(ge=-45, le=45)]
    substrateMaterial: Literal["Si"]


Vector3 = Annotated[list[Number], Field(min_length=3, max_length=3)]
Vector2 = Annotated[list[Number], Field(min_length=2, max_length=2)]
DirectionVector3 = Annotated[list[Annotated[Number, Field(ge=-1, le=1)]], Field(min_length=3, max_length=3)]


class Camera3D(StrictModel):
    position: Vector3
    focalPoint: Vector3
    viewUp: Vector3
    parallelScale: Annotated[Number, Field(gt=0)]

    @model_validator(mode="after")
    def nondegenerate(self) -> "Camera3D":
        direction = [a-b for a,b in zip(self.position,self.focalPoint)]
        cross = [direction[1]*self.viewUp[2]-direction[2]*self.viewUp[1], direction[2]*self.viewUp[0]-direction[0]*self.viewUp[2], direction[0]*self.viewUp[1]-direction[1]*self.viewUp[0]]
        if sum(x*x for x in cross) < 1e-24:
            raise ValueError("camera position, focalPoint and viewUp must define a camera frame")
        return self


class Clip3D(StrictModel):
    enabled: bool
    axis: Literal["x", "y", "z"]
    position: Number
    keep: Literal["positive", "negative"]


class Section3D(StrictModel):
    enabled: bool
    axis: Literal["x", "y", "z"]
    position: Number


class View3D(StrictModel):
    projection: Literal["parallel", "perspective"] = "parallel"
    camera: Camera3D | None = None
    displayMode: Literal["surface", "wireframe", "surfaceEdges"] = "surface"
    visibleMaterialIds: list[Integer] = Field(default_factory=lambda: [0,10,30], max_length=3)
    opacityByMaterial: dict[str, Annotated[Number, Field(ge=0, le=1)]] = Field(default_factory=lambda: {"0":1.,"10":1.,"30":1.})
    clip: Clip3D = Field(default_factory=lambda: Clip3D(enabled=False,axis="x",position=0.,keep="positive"))
    section: Section3D = Field(default_factory=lambda: Section3D(enabled=False,axis="y",position=0.))

    @model_validator(mode="after")
    def materials(self) -> "View3D":
        if any(x not in (0,10,30) for x in self.visibleMaterialIds) or any(x not in ("0","10","30") for x in self.opacityByMaterial):
            raise ValueError("view material IDs must be Mask=0, Si=10 or SiO2=30")
        return self


class ProjectV2(StrictModel):
    schemaVersion: Literal[2]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    dimension: Literal[3]
    units: UnitsLegacy
    domain: Domain3D
    geometry: Annotated[Union[PlaneGeometry, TrenchGeometry, HoleGeometry], Field(discriminator="kind")]
    steps: list[Step]
    solver: Solver
    view3D: View3D

    @model_validator(mode="after")
    def unique_step_ids(self) -> "ProjectV2":
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step IDs must be unique")
        return self


class View3DV3(StrictModel):
    projection: Literal["parallel", "perspective"] = "parallel"
    camera: Camera3D | None = None
    displayMode: Literal["surface", "wireframe", "surfaceEdges"] = "surface"
    visibleMaterialIds: list[Integer] = Field(default_factory=list, max_length=16)
    opacityByMaterial: dict[str, Annotated[Number, Field(ge=0, le=1)]] = Field(default_factory=dict)
    clip: Clip3D = Field(default_factory=lambda: Clip3D(enabled=False,axis="x",position=0.,keep="positive"))
    section: Section3D = Field(default_factory=lambda: Section3D(enabled=False,axis="y",position=0.))


class ProjectV3(StrictModel):
    schemaVersion: Literal[3]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    dimension: Literal[2, 3]
    units: UnitsLegacy
    domain: Domain | Domain3D
    geometry: dict
    steps: list[dict]
    solver: Solver
    view: View | None = None
    view3D: View3DV3 | None = None
    materialDefinitions: Annotated[list[MaterialDefinition], Field(max_length=64)] | None = None

    @model_validator(mode="before")
    @classmethod
    def select_dimension_domain(cls, data):
        return _select_dimension_domain(data, cls)

    @model_validator(mode="after")
    def version3_shape(self):
        if self.dimension == 2 and (self.view is None or self.view3D is not None):
            raise ValueError("2D schemaVersion 3 requires view and forbids view3D")
        if self.dimension == 3 and (self.view3D is None or self.view is not None):
            raise ValueError("3D schemaVersion 3 requires view3D and forbids view")
        if self.dimension == 2 and not isinstance(self.domain, Domain):
            raise ValueError("2D schemaVersion 3 requires the 2D domain")
        if self.dimension == 3 and not isinstance(self.domain, Domain3D):
            raise ValueError("3D schemaVersion 3 requires the 3D domain")
        # V3 geometry and step unions are validated by the explicit strict
        # validators below; dict fields preserve the v1/v2 model byte shape.
        normalized_geometry, normalized_steps = validate_v3_project_parts(self.geometry, self.steps, self.dimension)
        validate_geometry_native_flags(normalized_geometry, self.dimension,
                                       _project_boundary_name(self.domain))
        # Normalize validated numbers/defaults once, without triggering the
        # validate_assignment hook recursively. Legacy contracts are untouched.
        object.__setattr__(self, "geometry", normalized_geometry)
        object.__setattr__(self, "steps", normalized_steps)
        return self


def validate_v3_project_parts(geometry, steps, dimension):
    from pydantic import TypeAdapter
    parsed_geometry = TypeAdapter(GeometryV3).validate_python(geometry)
    parsed = [StepV3.model_validate(item) for item in steps]
    ids = [item.id for item in parsed]
    if len(ids) != len(set(ids)):
        raise ValueError("step IDs must be unique")
    return parsed_geometry.model_dump(mode="json", exclude_none=True), [item.model_dump(mode="json", exclude_none=True) for item in parsed]


def _project_boundary_name(domain: object) -> str:
    value = getattr(domain, "boundaryXY", None)
    if value is None:
        value = getattr(domain, "xBoundary", "reflective")
    return str(value)


def validate_geometry_native_flags(geometry: dict, dimension: int, boundary: str) -> None:
    """Reject native half-shape requests that the installed bindings ignore.

    ViennaPS 4.6.2 logs a warning and widens HALF/QUARTER holes (and ignores
    halfFin/halfTrench) when the domain has periodic boundaries. The Workbench
    never turns a requested half structure into a full one silently, so the
    combination fails here before the worker starts.
    """
    kind = geometry.get("kind")
    if kind == "hole":
        shape = geometry.get("holeShape")
        if shape in ("HALF", "QUARTER"):
            if dimension != 3:
                raise ValueError(f"holeShape {shape} is only applicable to 3D hole geometry")
            if boundary == "periodic":
                raise ValueError(
                    f"holeShape {shape} is not supported with periodic boundaries; "
                    "the installed ViennaPS binding silently widens it to FULL")
    elif kind == "trench" and geometry.get("halfTrench"):
        if boundary == "periodic":
            raise ValueError(
                "halfTrench is not supported with periodic boundaries; "
                "the installed ViennaPS binding silently ignores it")
    elif kind == "fin" and geometry.get("halfFin"):
        if boundary == "periodic":
            raise ValueError(
                "halfFin is not supported with periodic boundaries; "
                "the installed ViennaPS binding silently ignores it")


class PlaneV3(StrictModel):
    kind: Literal["plane"]
    height: Number
    substrateMaterial: str

class TrenchV3(StrictModel):
    kind: Literal["trench"]
    width: Annotated[Number, Field(gt=0)]
    depth: Annotated[Number, Field(gt=0)]
    taperAngle: Annotated[Number, Field(ge=-45, le=45)]
    maskHeight: Annotated[Number, Field(ge=0)]
    maskTaperAngle: Annotated[Number, Field(ge=-45, le=45)]
    substrateMaterial: str
    maskMaterial: str = "Mask"
    # Installed MakeTrench(..., halfTrench=True) halves the x domain so only
    # the x>=0 half is built. Omitted preserves the historical full trench.
    halfTrench: bool | None = None

class HoleV3(StrictModel):
    kind: Literal["hole"]
    radius: Annotated[Number, Field(gt=0)]
    depth: Annotated[Number, Field(gt=0)]
    taperAngle: Annotated[Number, Field(ge=-45, le=45)]
    maskHeight: Annotated[Number, Field(ge=0)]
    maskTaperAngle: Annotated[Number, Field(ge=-45, le=45)]
    substrateMaterial: str
    maskMaterial: str = "Mask"
    # Installed native ps.HoleShape. Omitted is FULL. HALF keeps x>=0 and
    # QUARTER keeps x>=0,y>=0; both are 3D-only and reject periodic
    # boundaries because the native constructor silently widens them to FULL.
    holeShape: Literal["FULL", "HALF", "QUARTER"] | None = None

class StackLayer(StrictModel):
    id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    material: str
    thickness: Annotated[Number, Field(gt=0)]
    role: Literal["film", "mask"]
    source: Literal["gds-mask"] | None = None

class StackPatternLayer(StrictModel):
    """One cutout in the installed native ``MakeTrench(MaterialLayer[])`` list.

    The entry is matched to one stack layer by zero-based ``layerIndex`` or by
    ``layerId``. Exactly one reference form is allowed. ``isMask`` is the
    native ``MaterialLayer.isMask`` flag: true cuts an opening through this
    layer, false leaves the layer intact. It is therefore a per-layer geometry
    flag, not the stack layer's etch-protection ``role``.
    """

    layerIndex: Annotated[Integer, Field(ge=0, le=15)] | None = None
    layerId: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")] | None = None
    width: Annotated[Number, Field(gt=0)]
    taperAngle: Annotated[Number, Field(ge=-45, le=45)]
    isMask: bool

    @model_validator(mode="after")
    def one_layer_reference(self):
        if (self.layerIndex is None) == (self.layerId is None):
            raise ValueError(
                "each per-layer cutout must reference exactly one stack layer "
                "with layerIndex or layerId")
        # The worker shares these helpers for direct (non-API) input.
        strict_finite_number(self.width, "width")
        strict_finite_number(self.taperAngle, "taperAngle")
        if self.layerIndex is not None:
            strict_integer(self.layerIndex, "layerIndex")
        strict_boolean(self.isMask, "isMask")
        return self

class StackPattern(StrictModel):
    kind: Literal["none", "trench", "hole"]
    width: Annotated[Number, Field(gt=0)] | None = None
    radius: Annotated[Number, Field(gt=0)] | None = None
    taperAngle: Annotated[Number, Field(ge=-45, le=45)] | None = None
    # Omitted keeps the historical top-layer stencil and its request hash.
    layers: Annotated[list[StackPatternLayer], Field(min_length=1, max_length=16)] | None = None

    @model_validator(mode="after")
    def pattern_shape(self):
        fields = set(self.model_fields_set)
        if self.kind in {"none", "gds"}:
            if fields != {"kind"}:
                raise ValueError(f"{self.kind} pattern has no dimensions")
            return self
        if self.kind == "trench":
            if "layers" in fields:
                allowed = {"kind", "layers", "width", "taperAngle"}
                if not {"kind", "layers"} <= fields or not fields <= allowed:
                    raise ValueError(
                        "per-layer trench patterns accept only kind, layers and "
                        "the optional legacy width/taperAngle pair")
                if ("width" in fields) != ("taperAngle" in fields):
                    raise ValueError(
                        "legacy width and taperAngle must be supplied together "
                        "or omitted when per-layer cutouts are used")
            elif fields != {"kind", "width", "taperAngle"}:
                raise ValueError("trench pattern requires exactly width and taperAngle")
            if self.layers is None and (self.width is None or self.taperAngle is None):
                raise ValueError("trench pattern requires width and taperAngle")
            return self
        if "layers" in fields:
            raise ValueError(
                "per-layer holes are not supported: installed MakeHole has no "
                "MaterialLayer[] overload")
        if fields != {"kind", "radius", "taperAngle"}:
            raise ValueError("hole pattern requires exactly radius and taperAngle")
        if self.radius is None or self.taperAngle is None:
            raise ValueError("hole pattern requires radius and taperAngle")
        return self

class StackPatternV7(StackPattern):
    kind: Literal["none", "trench", "hole", "gds"]

class StackV3(StrictModel):
    kind: Literal["stack"]
    substrateMaterial: str
    baseHeight: Number = 0
    layers: Annotated[list[StackLayer], Field(max_length=16)]
    pattern: StackPattern

    @model_validator(mode="after")
    def stack_constraints(self):
        ids = [x.id for x in self.layers]
        if len(ids) != len(set(ids)): raise ValueError("stack layer IDs must be unique")
        if self.pattern.kind != "none" and (not self.layers or self.layers[-1].role != "mask"):
            raise ValueError("an opening requires the top layer to have role=mask")
        if not self.layers and self.pattern.kind != "none": raise ValueError("an empty stack only allows pattern none")
        gds_layers = [index for index, layer in enumerate(self.layers) if layer.source == "gds-mask"]
        if len(gds_layers) > 1 or (gds_layers and (gds_layers[0] != len(self.layers) - 1 or self.pattern.kind != "gds")):
            raise ValueError("a GDS mask must be the unique top layer with top pattern gds")
        if self.pattern.kind == "gds" and not gds_layers:
            raise ValueError("top pattern gds requires a GDS mask layer")
        self._check_per_layer_pattern()
        return self

    def _check_per_layer_pattern(self) -> None:
        pattern = self.pattern
        if pattern.layers is None:
            return
        if pattern.kind != "trench":
            raise ValueError("per-layer cutouts are supported only for trench patterns")
        base_height = strict_finite_number(self.baseHeight, "baseHeight")
        if base_height < 0:
            raise ValueError("per-layer trench requires a non-negative baseHeight")
        by_id = {layer.id: index for index, layer in enumerate(self.layers)}
        resolved: dict[int, StackPatternLayer] = {}
        previous = -1
        for position, entry in enumerate(pattern.layers):
            if entry.layerIndex is not None:
                index = int(entry.layerIndex)
                if index >= len(self.layers):
                    raise ValueError(
                        f"pattern.layers[{position}].layerIndex {index} is outside "
                        f"the {len(self.layers)}-layer stack")
            else:
                assert entry.layerId is not None
                if entry.layerId not in by_id:
                    raise ValueError(
                        f"pattern.layers[{position}].layerId {entry.layerId!r} is "
                        "not present in the stack")
                index = by_id[entry.layerId]
            if index in resolved:
                raise ValueError("per-layer cutouts must reference each stack layer at most once")
            if index <= previous:
                raise ValueError("per-layer cutouts must be ordered from bottom to top")
            previous = index
            resolved[index] = entry
            if self.layers[index].source == "gds-mask":
                raise ValueError("a GDS mask layer cannot carry a per-layer native cutout")
        if set(resolved) != set(range(len(self.layers))) or len(resolved) != len(self.layers):
            raise ValueError(
                "per-layer trench must reference every stack layer exactly once "
                "from bottom to top; omit no layer, including the bottom film")
        top = len(self.layers) - 1
        if not resolved[top].isMask:
            raise ValueError("per-layer trench requires isMask=true on the top stack layer")
        if pattern.width is not None:
            if not math.isclose(float(pattern.width), float(resolved[top].width),
                                rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(
                    "legacy pattern.width must match the top per-layer cutout width")
            assert pattern.taperAngle is not None
            if not math.isclose(float(pattern.taperAngle), float(resolved[top].taperAngle),
                                rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(
                    "legacy pattern.taperAngle must match the top per-layer cutout taperAngle")

GeometryV3 = Annotated[Union[PlaneV3,TrenchV3,HoleV3,StackV3], Field(discriminator="kind")]


class FinExtrusionV7(StrictModel):
    extent: Vector2
    axis: Literal[1] = 1

    @model_validator(mode="after")
    def ordered_extent(self):
        if self.extent[0] >= self.extent[1]:
            raise ValueError("fin extrusion extent must have an increasing minimum and maximum")
        return self


class FinGeometryV7(StrictModel):
    kind: Literal["fin"]
    width: Annotated[Number, Field(gt=0)]
    height: Annotated[Number, Field(gt=0)]
    taperAngle: Annotated[Number, Field(ge=-45, le=45)] = 0
    maskHeight: Annotated[Number, Field(ge=0)] = 0
    maskTaperAngle: Annotated[Number, Field(ge=-45, le=45)] = 0
    substrateMaterial: str
    maskMaterial: str = "Mask"
    construction: Literal["native", "boolean"] = "native"
    extrusion: FinExtrusionV7 | None = None
    # Installed MakeFin(..., halfFin=True) halves the x domain (x>=0 only).
    # Omitted preserves the historical full fin.
    halfFin: bool | None = None


class GDSLayerV7(StrictModel):
    layer: Annotated[Integer, Field(ge=0, le=65535)]
    material: str
    baseHeight: Number | None = None
    height: Annotated[Number, Field(gt=0)] | None = None
    mask: bool | None = None


class GDSMaskPatternV7(StrictModel):
    """A GDS polygon layer used as a physical mask over an existing 3D stack."""
    name: Annotated[str, Field(min_length=1, max_length=80)] = "GDS Mask"
    assetId: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    fileName: Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[^/\\]+$")]
    structure: Annotated[str, Field(min_length=1, max_length=512)]
    layer: Annotated[Integer, Field(ge=0, le=65535)]
    datatype: Annotated[Integer, Field(ge=0, le=32767)] = 0
    unitScale: Annotated[Number, Field(gt=0, le=1000000)] = 1
    position: Vector3 = [0, 0, 0]
    baseHeight: Number = 1.2
    height: Annotated[Number, Field(gt=0)] = 0.3
    material: str = "Mask"
    retainedRegion: Literal["inside", "outside"] = "inside"
    protectsEtch: bool = True


class StackV7(StackV3):
    pattern: StackPatternV7
    gdsMask: GDSMaskPatternV7 | None = None

    @model_validator(mode="after")
    def per_layer_excludes_gds_mask(self):
        # The native per-layer constructor clears and rebuilds the whole
        # domain, while the GDS overlay is inserted afterwards from a layout.
        # Combining them would silently discard one of the two geometries.
        if self.pattern.layers is not None and self.gdsMask is not None:
            raise ValueError("per-layer trench cannot be combined with a GDS mask overlay")
        return self


class GDSBlurV7(StrictModel):
    """Optional native GDS proximity blur (``GDSGeometry.addBlur``).

    This is the installed layout proximity blur: Gaussian sigmas on an exposure
    grid, a normalized weighted sum and one threshold contour. It is **not** a
    resist model, OPC or three-dimensional lithography simulation. ``sigmas``
    use the native unit (grid intervals, i.e. ``sigma * gridDelta`` um), exactly
    like the shipped ``examples/GDSReader/GDSReader.py``. ``delta`` is the
    exposure grid spacing and defaults to the project gridDelta.
    """

    enabled: bool = True
    sigmas: Annotated[list[Annotated[Number, Field(gt=0)]],
                      Field(min_length=1, max_length=4)] = Field(default_factory=list)
    weights: Annotated[list[Annotated[Number, Field(ge=0)]],
                       Field(min_length=1, max_length=4)] = Field(default_factory=list)
    threshold: Annotated[Number, Field(gt=0, lt=1)] = 0.5
    delta: Annotated[Number, Field(gt=0)] | None = None
    gridRefine: Annotated[Integer, Field(ge=1, le=8)] = 4

    @model_validator(mode="after")
    def checked_sampling(self):
        if not self.enabled:
            return self
        if not self.sigmas or not self.weights:
            raise ValueError("GDS blur requires nonempty sigmas and weights")
        if len(self.sigmas) != len(self.weights):
            raise ValueError("GDS blur sigmas and weights must have the same length")
        total = 0.0
        for weight in self.weights:
            if not math.isfinite(weight):
                raise ValueError("GDS blur weights must be finite")
            total += weight
            if not math.isfinite(total):
                raise ValueError("GDS blur weights sum exceeds the finite range")
        if total <= 0:
            raise ValueError("GDS blur weights must have a positive sum")
        return self


class GDSGeometryV7(StrictModel):
    kind: Literal["gds"]
    assetId: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    fileName: Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[^/\\\\]+$")]
    structure: Annotated[str, Field(min_length=1, max_length=512)]
    unitScale: Annotated[Number, Field(gt=0, le=1000000)] = 1
    position: Vector3 = [0, 0, 0]
    layers: Annotated[list[GDSLayerV7], Field(min_length=1, max_length=64)]
    blur: GDSBlurV7 | None = None

    @model_validator(mode="after")
    def unique_layers(self):
        values = [row.layer for row in self.layers]
        if len(values) != len(set(values)):
            raise ValueError("GDS layer mappings must use unique layer IDs")
        return self


GeometryV7 = Annotated[Union[PlaneV3,TrenchV3,HoleV3,StackV7,FinGeometryV7,GDSGeometryV7], Field(discriminator="kind")]


def normalized_geometry_v3(project: object) -> dict:
    """Return the canonical native-geometry representation for schemas 1–7."""
    from pydantic import TypeAdapter

    raw = getattr(project, "geometry")
    geometry = raw if isinstance(raw, dict) else raw.model_dump(mode="json", exclude_none=True)
    geometry = dict(geometry)
    if geometry.get("kind") in ("trench", "hole"):
        geometry.setdefault("maskMaterial", "Mask")
    schema_version = getattr(project, "schemaVersion", 1)
    adapter = GeometryV7 if schema_version == 7 else GeometryV3
    normalized = TypeAdapter(adapter).validate_python(geometry).model_dump(mode="json", exclude_none=True)
    # Keep historical schema-7 project hashes stable when the Fin used the
    # original native MakeFin constructor (the default before this field).
    if normalized.get("kind") == "fin" and normalized.get("construction", "native") == "native":
        normalized.pop("construction", None)
    return normalized

class StepV3(StrictModel):
    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    operation: Literal["etch", "deposit"]
    model: Literal["isotropic", "singleParticle"]
    rateMagnitude: Annotated[Number, Field(ge=0, le=2)]
    duration: Annotated[Number, Field(ge=0.01, le=600)]
    depositMaterial: str | None = None
    stickingProbability: Annotated[Number, Field(ge=0, le=1)] | None = None
    sourceExponent: Annotated[Number, Field(ge=0, le=20)] | None = None

    @model_validator(mode="after")
    def validate_model_fields(self):
        if self.operation == "deposit" and self.depositMaterial is None: raise ValueError("deposit steps require depositMaterial")
        if self.operation == "etch" and "depositMaterial" in self.model_fields_set: raise ValueError("depositMaterial applies only to deposit steps")
        if self.model == "singleParticle" and (self.stickingProbability is None or self.sourceExponent is None): raise ValueError("singleParticle requires stickingProbability and sourceExponent")
        if self.model == "isotropic" and ({"stickingProbability","sourceExponent"} & self.model_fields_set): raise ValueError("particle parameters apply only to singleParticle")
        return self


class StepV4(StrictModel):
    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    operation: Literal["etch", "deposit"]
    model: Literal["isotropic", "singleParticle"]
    rateMagnitude: Annotated[Number, Field(ge=0, le=2)]
    duration: Annotated[Number, Field(ge=0.01, le=600)]
    depositMaterial: str | None = None
    stickingProbability: Annotated[Number, Field(ge=0, le=1)] | None = None
    sourceExponent: Annotated[Number, Field(ge=0, le=20)] | None = None
    rateMode: Literal["uniform", "material"] = "uniform"
    materialRates: dict[str, Annotated[Number, Field(ge=0, le=2)]] | None = None
    selectivityReference: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    # Isotropic dispatch. ``None`` rather than a default keeps both fields out of
    # ``model_dump(exclude_none=True)`` (the canonical request hash input) for
    # every existing step, which keeps historical request hashes unchanged.
    # ``auto`` is resolved by the worker from the project dimension and the
    # geometric safety rules; validation only has to admit it.
    isotropicMethod: Literal["auto", "levelSet", "geometric"] | None = None
    isotropicTimeStepRatio: Annotated[Number, Field(ge=0.05, le=0.5)] | None = None
    #: Optional native advection override for this step. Omitted keeps the
    #: project solver default (and stays out of the canonical request hash).
    advection: AdvectionOverride | None = None

    @model_validator(mode="after")
    def validate_rate_and_model_fields(self):
        if self.operation == "deposit" and self.depositMaterial is None:
            raise ValueError("deposit steps require depositMaterial")
        if self.operation == "etch" and "depositMaterial" in self.model_fields_set:
            raise ValueError("depositMaterial applies only to deposit steps")
        if self.model == "singleParticle" and (self.stickingProbability is None or self.sourceExponent is None):
            raise ValueError("singleParticle requires stickingProbability and sourceExponent")
        if self.model == "isotropic" and ({"stickingProbability", "sourceExponent"} & self.model_fields_set):
            raise ValueError("particle parameters apply only to singleParticle")
        if self.model != "isotropic" and ({"isotropicMethod", "isotropicTimeStepRatio"} & self.model_fields_set):
            raise ValueError("isotropicMethod and isotropicTimeStepRatio apply only to isotropic steps")
        if self.model != "isotropic" and self.advection is not None:
            raise ValueError("advection override applies only to isotropic steps in this schema")
        if self.advection is not None and self.isotropicTimeStepRatio is not None:
            requested = self.advection.timeStepRatio
            if requested is not None and abs(float(requested) - float(self.isotropicTimeStepRatio)) > 1e-12:
                raise ValueError(
                    "advection.timeStepRatio and isotropicTimeStepRatio disagree; "
                    "set only one step-level ratio")
        if self.isotropicMethod == "geometric" and self.rateMode == "material":
            # A geometric advection cannot carry a per-material rate table: it
            # offsets the whole exposed surface by one uniform radius.
            raise ValueError("isotropicMethod geometric is not supported with rateMode material")
        if self.rateMode == "uniform" and "materialRates" in self.model_fields_set:
            raise ValueError("uniform rateMode forbids materialRates")
        if self.rateMode == "material":
            if "materialRates" not in self.model_fields_set or self.materialRates is None:
                raise ValueError("material rateMode requires materialRates, which may be empty")
        return self


class ProjectV4(StrictModel):
    schemaVersion: Literal[4]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    dimension: Literal[2, 3]
    units: UnitsLegacy
    domain: Domain | Domain3D
    geometry: dict
    steps: list[StepV4]
    solver: Solver
    view: View | None = None
    view3D: View3DV3 | None = None
    materialDefinitions: Annotated[list[MaterialDefinition], Field(max_length=64)] | None = None

    @model_validator(mode="before")
    @classmethod
    def select_dimension_domain(cls, data):
        return _select_dimension_domain(data, cls)

    @model_validator(mode="after")
    def version4_shape(self):
        if self.dimension == 2 and (self.view is None or self.view3D is not None or not isinstance(self.domain,Domain)):
            raise ValueError("2D schemaVersion 4 requires 2D domain and view only")
        if self.dimension == 3 and (self.view3D is None or self.view is not None or not isinstance(self.domain,Domain3D)):
            raise ValueError("3D schemaVersion 4 requires 3D domain and view3D only")
        from pydantic import TypeAdapter
        geometry=TypeAdapter(GeometryV3).validate_python(self.geometry)
        normalized_geometry=geometry.model_dump(mode="json",exclude_none=True)
        validate_geometry_native_flags(normalized_geometry, self.dimension,
                                       _project_boundary_name(self.domain))
        object.__setattr__(self,"geometry",normalized_geometry)
        ids=[step.id for step in self.steps]
        if len(ids)!=len(set(ids)): raise ValueError("step IDs must be unique")
        return self

Project = ProjectV1  # frozen v1 public Python API


def _directional_rate_set_directions(step: "DirectionalStepV5") -> list[tuple[int | None, list[float]]]:
    """Return the per-set (index, direction) pairs a 2D project must check."""
    if step.rateSets is not None:
        return [(index, rate_set.direction) for index, rate_set in enumerate(step.rateSets)]
    return [(None, step.direction)]


# A 2D direction is projected onto [x, h] by the schema and by the worker's
# normalization. Checking the raw third component with an absolute tolerance is
# not enough: [0, -1e-12, 1e-12] passes an absolute <= 1e-12 test but normalizes
# to [0, -0.707, 0.707], i.e. a mostly out-of-plane beam. Compare the normalized
# third component instead so the tolerance is relative to the vector length.
PLANAR_DIRECTION_TOLERANCE = 1e-12


def normalized_out_of_plane_component(direction: list[float]) -> float:
    length = math.sqrt(sum(float(value) ** 2 for value in direction))
    if length <= 0.0:
        return 0.0
    return abs(float(direction[2])) / length


class DirectionalRateSetV5(StrictModel):
    """One real ``viennaps.RateSet`` of the native multi-set overload.

    ``directionalVelocity`` and ``isotropicVelocity`` are non-negative rate
    magnitudes in the declared rate unit, exactly like the step-level scalar fields: the step
    ``operation`` selects the native sign (etch negative, deposit positive).
    The name follows the native RateSet property and is not a signed raw value.
    """

    direction: DirectionVector3
    directionalVelocity: Annotated[Number, Field(ge=0, le=2)]
    isotropicVelocity: Annotated[Number, Field(ge=0, le=2)] = 0.0
    # ``None`` means "derive from the project geometry", the same protected
    # materials the scalar directional path uses. An explicit list (including
    # the empty list) is honoured verbatim, so a set can only skip protection
    # by naming that intent explicitly.
    maskMaterials: Annotated[list[Annotated[str, Field(min_length=1, max_length=80)]], Field(max_length=16)] | None = None
    calculateVisibility: bool = True

    @model_validator(mode="after")
    def validate_rate_set(self):
        if math.sqrt(sum(float(value) ** 2 for value in self.direction)) <= 1e-12:
            raise ValueError("rateSets direction must be a nonzero 3-component vector")
        if self.maskMaterials is not None and len(set(self.maskMaterials)) != len(self.maskMaterials):
            raise ValueError("rateSets maskMaterials must not contain duplicate material names")
        return self


class DirectionalMaterialRate(StrictModel):
    """One native ``RateMap`` entry.

    In the default ``rateSigns="operation"`` mode both values are non-negative
    requested magnitudes and the step ``operation`` supplies the native sign.
    ``rateSigns="native"`` passes the values verbatim, which is required to
    translate a native script such as the FinFET example that mixes a positive
    directional etch rate with a positive isotropic growth rate. A material
    absent from the table falls back to ``DirectionalStepV5.defaultMaterialRate``.
    """

    directional: Annotated[Number, Field(ge=-2, le=2)] = 0.0
    isotropic: Annotated[Number, Field(ge=-2, le=2)] = 0.0


class DirectionalStepV5(StrictModel):
    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    operation: Literal["etch", "deposit"]
    model: Literal["directional"]
    # Scalar mode keeps the historical required fields. ``None`` rather than a
    # default keeps them out of ``model_dump(exclude_none=True)`` for the
    # additive ``rateSets`` mode, so every existing request hash is unchanged.
    direction: DirectionVector3 | None = None
    directionalRateMagnitude: Annotated[Number, Field(ge=0, le=2)] | None = None
    isotropicRateMagnitude: Annotated[Number, Field(ge=0, le=2)] | None = None
    calculateVisibility: bool = True
    # Native DirectionalProcess(rateSets: Sequence[RateSet]). Bounded because
    # every set with a directed component costs one native visibility pass.
    rateSets: Annotated[list[DirectionalRateSetV5], Field(min_length=1, max_length=8)] | None = None
    # Native DirectionalProcess(direction, RateMap, default directional rate,
    # default isotropic rate) overload. Both fields default to ``None`` so an
    # absent table never enters the canonical request hash.
    materialRates: dict[str, DirectionalMaterialRate] | None = None
    defaultMaterialRate: DirectionalMaterialRate | None = None
    #: ``native`` passes the table to the native constructor unchanged. Omitted
    #: keeps the historical operation-sign mapping and stays out of the hash.
    rateSigns: Literal["operation", "native"] | None = None
    duration: Annotated[Number, Field(ge=0.01, le=600)]
    depositMaterial: str | None = None
    advection: AdvectionOverride | None = None

    @model_validator(mode="after")
    def validate_directional_fields(self):
        scalar_fields = ("direction", "directionalRateMagnitude", "isotropicRateMagnitude")
        # A material table is the mode switch; an explicit JSON null is treated
        # like an omitted field so clients that always serialize the key keep
        # the historical scalar/rateSets behaviour.
        material_mode = self.materialRates is not None or self.defaultMaterialRate is not None
        if self.rateSigns is not None and not material_mode:
            raise ValueError("rateSigns applies only to materialRates mode")
        if material_mode:
            table = self.materialRates or {}
            if "directionalRateMagnitude" in self.model_fields_set or "isotropicRateMagnitude" in self.model_fields_set:
                raise ValueError(
                    "materialRates is mutually exclusive with directionalRateMagnitude and "
                    "isotropicRateMagnitude: omit both scalar fields in material mode")
            if self.rateSets is not None:
                raise ValueError("materialRates is mutually exclusive with rateSets")
            if self.direction is None:
                raise ValueError("material rate mode requires direction")
            if math.sqrt(sum(float(value) ** 2 for value in self.direction)) <= 1e-12:
                raise ValueError("direction must be a nonzero 3-component vector")
            if self.calculateVisibility is not True:
                raise ValueError(
                    "material rate mode has no native visibility control; omit calculateVisibility "
                    "instead of materializing it (or leave it true)")
            if self.rateSigns != "native":
                negative = sorted(
                    name for name, row in table.items()
                    if float(row.directional) < 0.0 or float(row.isotropic) < 0.0)
                if self.defaultMaterialRate is not None and (
                    float(self.defaultMaterialRate.directional) < 0.0
                    or float(self.defaultMaterialRate.isotropic) < 0.0
                ):
                    negative.append("defaultMaterialRate")
                if negative:
                    raise ValueError(
                        "material rates must be non-negative unless rateSigns='native': "
                        + ", ".join(negative))
            default = self.defaultMaterialRate
            effective = any(
                float(row.directional) != 0.0 or float(row.isotropic) != 0.0 for row in table.values()
            ) or (default is not None
                  and (float(default.directional) != 0.0 or float(default.isotropic) != 0.0))
            if not effective:
                raise ValueError(
                    "material rate mode requires at least one nonzero directional or isotropic rate")
        elif self.rateSets is None:
            if any(getattr(self, name) is None for name in scalar_fields):
                raise ValueError(
                    "directional steps require direction, directionalRateMagnitude and "
                    "isotropicRateMagnitude unless rateSets is used")
            if math.sqrt(sum(float(value) ** 2 for value in self.direction)) <= 1e-12:
                raise ValueError("direction must be a nonzero 3-component vector")
        else:
            if any(name in self.model_fields_set for name in scalar_fields):
                raise ValueError(
                    "rateSets is mutually exclusive with direction, directionalRateMagnitude "
                    "and isotropicRateMagnitude: omit all three fields in rateSets mode; send them "
                    "only for scalar directional steps, never as 0 placeholders")
            if self.calculateVisibility is not True:
                raise ValueError(
                    "rateSets uses per-set calculateVisibility; omit the step-level flag "
                    "instead of materializing it (or leave it true)")
        if self.operation == "deposit" and self.depositMaterial is None:
            raise ValueError("deposit steps require depositMaterial")
        if self.operation == "etch" and "depositMaterial" in self.model_fields_set:
            raise ValueError("depositMaterial applies only to deposit steps")
        return self


class RemoveMaterialStepV5(StrictModel):
    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    model: Literal["removeMaterial"]
    material: Annotated[str, Field(min_length=1, max_length=80)]


class PlanarizeStepV5(StrictModel):
    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    model: Literal["planarize"]
    # A planarize step either cuts at an explicit height (the historical shape,
    # unchanged so old projects and their request hashes stay valid) or derives
    # that height at run time from the measured top of one material.
    # ``None`` rather than 0 keeps both new fields out of
    # ``model_dump(exclude_none=True)``, which is the canonical request hash
    # input, for every step that does not use them.
    cutoffHeight: Number | None = None
    stopMaterial: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    overPolish: Annotated[Number, Field(ge=0, le=100)] | None = None

    @model_validator(mode="after")
    def validate_cutoff_source(self):
        if (self.cutoffHeight is None) == (self.stopMaterial is None):
            raise ValueError("planarize requires exactly one of cutoffHeight or stopMaterial")
        if self.overPolish is not None and self.stopMaterial is None:
            raise ValueError("overPolish applies only to stopMaterial planarize steps")
        return self


class BooleanStepV5(StrictModel):
    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    model: Literal["boolean"]
    booleanOperation: Literal["union", "intersection", "difference"]
    # Position is [x,h,0] in 2D and [x,y,z] in 3D; z is vertical in 3D.
    position: Vector3
    width: Annotated[Number, Field(gt=0, le=40000)]
    height: Annotated[Number, Field(gt=0, le=100000)]
    depth: Annotated[Number, Field(gt=0, le=40000)] | None = None
    angle: Annotated[Number, Field(ge=-45, le=45)] = 0.0
    applyToAll: bool = True


StepV5 = Annotated[Union[StepV4, DirectionalStepV5, RemoveMaterialStepV5, PlanarizeStepV5, BooleanStepV5], Field(discriminator="model")]


ParticleLabel = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
Probability = Annotated[Number, Field(ge=0, le=1)]
RateCoefficient = Annotated[Number, Field(ge=0, le=2)]


class IonParticleV7(StrictModel):
    kind: Literal["ion"]
    label: ParticleLabel
    sourcePower: Annotated[Number, Field(ge=0, le=20)]
    thetaRMin: Annotated[Number, Field(ge=0, le=90)] = 0
    thetaRMax: Annotated[Number, Field(ge=0, le=90)] = 90
    minAngle: Annotated[Number, Field(ge=0, le=90)] = 80
    B_sp: Annotated[Number, Field(ge=-1, le=20)] = -1
    meanEnergy: Annotated[Number, Field(ge=0, le=1_000_000)] = 0
    sigmaEnergy: Annotated[Number, Field(ge=0, le=1_000_000)] = 0
    thresholdEnergy: Annotated[Number, Field(ge=0, le=1_000_000)] = 0
    inflectAngle: Annotated[Number, Field(ge=0, le=90)] = 0
    n: Annotated[Integer, Field(ge=1, le=100)] = 1

    @model_validator(mode="after")
    def check_ion_angles(self):
        if self.thetaRMin > self.thetaRMax:
            raise ValueError("thetaRMin must not exceed thetaRMax")
        return self


class NeutralParticleV7(StrictModel):
    kind: Literal["neutral"]
    label: ParticleLabel
    defaultStickingProbability: Probability = 1
    stickingProbabilityByMaterial: dict[str, Probability] = Field(default_factory=dict)


ParticleSpeciesV7 = Annotated[Union[IonParticleV7, NeutralParticleV7], Field(discriminator="kind")]


class MultiParticleRateCoefficientsV7(StrictModel):
    # Returned native velocity = operation sign * sum(flux[i] * coefficient[i]).
    # Native flux units are not declared by the installed binding; coefficients
    # are therefore explicit conversion factors supplied by the project author.
    defaultBySpecies: dict[ParticleLabel, RateCoefficient]
    byMaterial: dict[str, dict[ParticleLabel, RateCoefficient]] = Field(default_factory=dict)


class MultiParticleStepV7(StrictModel):
    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    operation: Literal["etch", "deposit"]
    model: Literal["multiParticle"]
    duration: Annotated[Number, Field(ge=0.01, le=600)]
    depositMaterial: str | None = None
    species: Annotated[list[ParticleSpeciesV7], Field(min_length=1, max_length=8)]
    rateCoefficients: MultiParticleRateCoefficientsV7

    @model_validator(mode="after")
    def check_multi_particle_fields(self):
        if self.operation == "deposit" and self.depositMaterial is None:
            raise ValueError("multi-particle deposit steps require depositMaterial")
        if self.operation == "etch" and "depositMaterial" in self.model_fields_set:
            raise ValueError("depositMaterial applies only to deposit steps")
        labels = [species.label for species in self.species]
        if len(labels) != len(set(labels)):
            raise ValueError("multi-particle species labels must be unique")
        label_set = set(labels)
        if set(self.rateCoefficients.defaultBySpecies) != label_set:
            raise ValueError("defaultBySpecies must contain exactly one coefficient per species label")
        for material, coefficients in self.rateCoefficients.byMaterial.items():
            if not material or set(coefficients) - label_set:
                raise ValueError(f"material rate coefficients for {material!r} contain an unknown species label")
        return self


class SurfaceDiffusionV7(StrictModel):
    """Native ``SurfaceDiffusionParameters`` smoothing of ALD coverage.

    ViennaPS 4.6.2 builds this Laplacian stencil only inside the ALP strategy,
    and the ALP flag is set solely by a positive
    ``SingleParticleALDParams.coverageDiffusionCoefficient``. It therefore
    smooths the scalar coverage field (which drives the ALD growth rate), not
    the level-set geometry, and is accepted only on ``specialized:ald`` steps.
    Defaults are the installed native struct defaults, so materializing them is
    a behaviour no-op relative to omitting the object.
    """

    enabled: bool = True
    kNeighbors: Annotated[Integer, Field(ge=1, le=64)] = 16
    normalCutoff: Annotated[Number, Field(ge=-1, le=1)] = 0.25
    normalizeByLocalScale: bool = True
    radius: Annotated[Number, Field(ge=0, le=2)] = 0.0
    sigmaNormal: Annotated[Number, Field(gt=0, le=10)] = 0.35
    stabilityFactor: Annotated[Number, Field(gt=0, le=1)] = 1.0
    symmetrizeWeights: bool = True

    @model_validator(mode="after")
    def check_disabled_matches_defaults(self) -> "SurfaceDiffusionV7":
        if not self.enabled:
            defaults = type(self)()
            overridden = sorted(
                key for key in self.model_fields_set
                if key != "enabled" and getattr(self, key) != getattr(defaults, key))
            if overridden:
                raise ValueError(
                    "disabled surfaceDiffusion must not override "
                    f"{overridden}; omit the object instead")
        return self


class OxidationAdvancedV7(StrictModel):
    """Optional native solver controls for ``specialized:oxidation``.

    The installed ViennaPS 4.6.2 ``Oxidation`` binding exposes many setters but
    no getters. This block carries only members proven to change the returned
    surface deterministically. Defaults are the installed native defaults, so an
    explicitly materialised block is a behaviour no-op relative to omitting it
    (the request hash still changes because the request text differs).

    Tightening ``mechanicsTolerance`` below the native default (0.01) or
    ``couplingTolerance`` below 0.05 is not exposed: controlled probes stopped
    converging. ``couplingTolerance`` must stay at least ``mechanicsTolerance``
    because the native validator already warns about that ordering.
    """

    cflFactor: Annotated[Number, Field(ge=0.01, le=0.499)] = 0.499
    mechanicsTolerance: Annotated[Number, Field(ge=0.01, le=0.2)] = 0.01
    couplingTolerance: Annotated[Number, Field(ge=0.05, le=0.5)] = 0.05
    simplePressureRelaxation: Annotated[Number, Field(ge=0.01, le=1.0)] = 0.5

    @model_validator(mode="after")
    def check_coupled_tolerances(self) -> "OxidationAdvancedV7":
        if float(self.couplingTolerance) < float(self.mechanicsTolerance):
            raise ValueError(
                "couplingTolerance must be greater than or equal to mechanicsTolerance")
        return self


class SpecializedStepV7(StrictModel):
    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    operation: Literal["etch", "deposit", "native"]
    model: Literal["specialized"]
    nativeModel: Annotated[str, Field(min_length=1, max_length=40)]
    duration: Annotated[Number, Field(ge=0.01, le=600)] | None = None
    depositMaterial: str | None = None
    nativeParameters: dict[str, Any]
    surfaceDiffusion: SurfaceDiffusionV7 | None = None
    advancedOxidation: OxidationAdvancedV7 | None = None
    #: Step-level native advection override. Only selective epitaxy needs it
    #: today (the FinFET example's stencil-local scheme); the other specialized
    #: models keep the project default.
    advection: AdvectionOverride | None = None

    @model_validator(mode="after")
    def check_specialized_shape(self):
        from .specialized_models import model_spec
        spec = model_spec(self.nativeModel)
        if spec is None:
            raise ValueError(f"unknown installed ViennaPS specialized model {self.nativeModel!r}")
        if self.operation != spec["operation"]:
            raise ValueError(f"{self.nativeModel} requires operation={spec['operation']!r}")
        if self.advection is not None and self.nativeModel != "selectiveEpitaxy":
            raise ValueError("advection override is only available for specialized:selectiveEpitaxy")
        if self.nativeModel == "oxidation":
            if self.duration is not None:
                raise ValueError("oxidation duration comes from nativeParameters.timeHours; omit duration")
        elif self.duration is None:
            raise ValueError(f"{self.nativeModel} requires a physical process duration in seconds")
        expected_material = spec.get("depositMaterial")
        if expected_material is not None and self.depositMaterial != expected_material:
            raise ValueError(f"{self.nativeModel} deposits {expected_material}")
        if spec.get("depositMaterialSelectable") and (not isinstance(self.depositMaterial, str) or not self.depositMaterial):
            raise ValueError(f"{self.nativeModel} requires an output material")
        if spec.get("operation") != "deposit" and "depositMaterial" in self.model_fields_set:
            raise ValueError("depositMaterial applies only to a native deposition model")
        expected_parameters = {row["key"] for row in spec["fields"]}
        optional_parameters = set(spec.get("optionalKeys", ()))
        if set(self.nativeParameters) - optional_parameters != expected_parameters:
            raise ValueError(f"{self.nativeModel} nativeParameters must contain exactly {sorted(expected_parameters)}"
                             + (f" (optional: {sorted(optional_parameters)})" if optional_parameters else ""))
        if self.advancedOxidation is not None:
            # Placement is oxidation-only whenever the object exists, even if
            # every field still carries its native default.
            if self.nativeModel != "oxidation":
                raise ValueError(
                    "advancedOxidation is only available for specialized:oxidation; "
                    f"{self.nativeModel} has no native Oxidation solver controls")
        if self.surfaceDiffusion is not None:
            # Placement is ALD-only whenever the object exists, including a
            # disabled token: there is no other 4.6.2 process with a native
            # surface-diffusion flag, so accepting it elsewhere would make the
            # schema claim a capability the engine does not have.
            if self.nativeModel != "ald":
                raise ValueError(
                    "surfaceDiffusion is only available for specialized:ald; "
                    f"{self.nativeModel} has no native surface-diffusion flag")
            if self.surfaceDiffusion.enabled:
                # ViennaPS 4.6.2 only builds the stencil when the ALD surface
                # model reports a positive coverage diffusion coefficient;
                # a disabled token is inert and may keep a zero coefficient.
                coefficient = self.nativeParameters.get("coverageDiffusionCoefficient")
                try:
                    finite = isinstance(coefficient, (int, float)) and not isinstance(coefficient, bool) \
                        and math.isfinite(float(coefficient)) and float(coefficient) > 0.0
                except (TypeError, ValueError):
                    finite = False
                if not finite:
                    raise ValueError(
                        "enabled surfaceDiffusion requires nativeParameters.coverageDiffusionCoefficient > 0; "
                        "ViennaPS 4.6.2 never builds the stencil for zero")
        return self


class GdsBooleanMaskV7(StrictModel):
    """One selected GDS layer/datatype used as a boolean operand.

    The geometry kind ``gds`` geometry owns the asset; this step re-reads that
    same asset and layer, so there is no second upload path. ``position`` is an
    additional XY placement (and a z offset added to ``baseHeight``) relative
    to the geometry placement, so the zero default is exactly aligned.
    """

    layer: Annotated[Integer, Field(ge=0, le=65535)]
    datatype: Annotated[Integer, Field(ge=0, le=32767)] = 0
    unitScale: Annotated[Number, Field(gt=0, le=1000000)] = 1
    position: Vector3 = [0, 0, 0]
    baseHeight: Annotated[Number, Field(ge=0)]
    height: Annotated[Number, Field(gt=0)]
    retainedRegion: Literal["inside", "outside"] = "inside"


class GdsBooleanStepV7(StrictModel):
    """A boolean cut/add whose operand is a real 3D mask built from a GDS layer."""

    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    model: Literal["gdsBoolean"]
    booleanOperation: Literal["union", "intersection", "difference"]
    gdsMask: GdsBooleanMaskV7
    applyToAll: bool = True


class CsvProcessStepV7(StrictModel):
    """Native ``ps.CSVFileProcess`` with an uploaded ``RateGrid`` table.

    ``operation`` is ``native`` because the installed model has no etch/deposit
    flag: the sign of every CSV rate chooses the direction of material motion
    for the selected direction vector (negative rates etch, positive rates
    deposit for a downward direction). ``interpolationMode`` is mandatory.
    ``CUSTOM`` and ``setCustomInterpolator`` are deliberately not expressible.
    """

    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    operation: Literal["native"]
    model: Literal["csvProcess"]
    duration: Annotated[Number, Field(ge=0.01, le=600)]
    assetId: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    fileName: Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[^/\\\x00-\x1f\x7f]+$")]
    direction: DirectionVector3
    offset: Vector2
    isotropicComponent: Annotated[Number, Field(ge=0, le=100)] = 0.0
    directionalComponent: Annotated[Number, Field(ge=0, le=100)] = 1.0
    interpolationMode: Literal["LINEAR", "IDW"]
    idwNeighbors: Annotated[Integer, Field(ge=1, le=64)] | None = None
    calculateVisibility: bool = True

    @model_validator(mode="after")
    def check_csv_process_fields(self) -> "CsvProcessStepV7":
        if math.sqrt(sum(float(value) ** 2 for value in self.direction)) <= 1e-12:
            raise ValueError("csvProcess requires a nonzero direction vector")
        if any(abs(float(value)) > 1e6 for value in self.offset):
            raise ValueError("csvProcess offset must stay within ±1,000,000 um")
        if float(self.isotropicComponent) == 0.0 and float(self.directionalComponent) == 0.0:
            raise ValueError("csvProcess requires a nonzero isotropicComponent or directionalComponent")
        if self.interpolationMode == "IDW" and self.idwNeighbors is None:
            raise ValueError("csvProcess IDW interpolation requires idwNeighbors")
        if self.interpolationMode == "LINEAR" and self.idwNeighbors is not None:
            raise ValueError("idwNeighbors applies only to IDW interpolation")
        return self


ShapeAxis = Annotated[Number, Field(gt=0, le=2000)]


class ParticleShapeStepV7(StrictModel):
    """Native ``BoxDistribution`` / ``SphereDistribution`` geometry offset.

    These are ViennaLS geometric-advection distributions, not velocity models:
    ``ps.Process(domain, model, duration)`` ignores the duration and moves the
    exposed surface by exactly one radius / half-axis set. A masked *etch*
    through ``addMaskMaterial`` is the officially supported native combination.
    A masked *deposition* SIGSEGVs in 2D and 3D on 4.6.2 and 4.7.0, so
    depositions stay maskless and validation rejects an explicit mask request.

    ``depositMaterial`` reproduces the native ``duplicateTopLevelSet(material)``
    that precedes a geometric deposition in the FinFET example. Omitted keeps
    the historical "move the current top level set only" behaviour. Omitted
    explicit ``maskMaterials`` keep deriving the mask from the project geometry;
    ``maskMode="allExcept"`` reproduces ``getMaterialsInDomain()`` minus the
    listed materials (the native spacer etch).
    """

    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    operation: Literal["etch", "deposit"]
    model: Literal["particleShape"]
    shape: Literal["box", "sphere"]
    radius: ShapeAxis | None = None
    halfAxes: Annotated[list[ShapeAxis], Field(min_length=2, max_length=3)] | None = None
    depositMaterial: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    maskMaterials: Annotated[list[Annotated[str, Field(min_length=1, max_length=80)]], Field(max_length=64)] | None = None
    maskMode: Literal["explicit", "allExcept"] | None = None

    @model_validator(mode="after")
    def check_shape_parameters(self) -> "ParticleShapeStepV7":
        if self.shape == "sphere":
            if self.radius is None:
                raise ValueError("sphere particleShape requires radius")
            if self.halfAxes is not None:
                raise ValueError("halfAxes applies only to shape=box")
        else:
            if self.halfAxes is None:
                raise ValueError("box particleShape requires halfAxes")
            if self.radius is not None:
                raise ValueError("radius applies only to shape=sphere")
        if self.operation == "etch" and self.depositMaterial is not None:
            raise ValueError("depositMaterial applies only to deposit particleShape steps")
        if self.maskMaterials is not None:
            if any(not name.strip() for name in self.maskMaterials):
                raise ValueError("maskMaterials entries must be non-empty material names")
            if len(set(self.maskMaterials)) != len(self.maskMaterials):
                raise ValueError("maskMaterials must not contain duplicate material names")
        if self.maskMode is not None and self.maskMaterials is None:
            raise ValueError("maskMode requires an explicit maskMaterials list")
        if self.operation == "deposit" and (self.maskMaterials is not None or self.maskMode is not None):
            raise ValueError(
                "particleShape.maskMaterials/maskMode are not supported for deposition: "
                "ViennaPS 4.6.2/4.7.0 SIGSEGV on a non-empty mask with positive geometric advection")
        return self


class StepCellSetV7(StrictModel):
    """The native ``DenseCellSet`` a cell-set field step runs on.

    ``viennaps.Domain.generateCellSet(position, coverMaterial, isAboveSurface)``
    is the only native constructor available (verified on 4.7.0). ``height`` is
    passed as the native ``position`` argument; measured on 4.7.0 that argument
    is the **absolute** top coordinate of the grid (a plane at ``z = 0.05`` with
    ``position = 0.5`` produced a grid topping out at ``0.50``). For the default
    plane at ``z = 0`` it therefore equals the height above the surface. The
    grid is captured at the first cell-set step and every later
    implant/anneal/analysis step reuses the same absolute grid (a surface-moving
    step in between regenerates the native cell set and conservatively remaps
    the scalar fields).
    """

    height: Annotated[Number, Field(gt=0, le=1000)]
    #: Depth of the finite substrate below the deepest surface point. A
    #: ViennaLS level set only stores a narrow band around the surface, so the
    #: native DenseCellSet would contain one substrate row without this. The
    #: worker widens the band on a deep copy of the Domain (the simulated
    #: surface itself is unchanged) before the native grid is generated.
    substrateDepth: Annotated[Number, Field(gt=0, le=1000)] | None = None
    coverMaterial: Annotated[str, Field(min_length=1, max_length=64)] = "GAS"
    periodic: bool = False
    format: Literal["vtu", "json", "both"] = "both"
    scalarLabels: list[str] | None = None

    @model_validator(mode="after")
    def check_scalar_labels(self) -> "StepCellSetV7":
        if self.scalarLabels is not None:
            if not self.scalarLabels:
                raise ValueError("scalarLabels must be omitted or a non-empty list")
            if len(set(self.scalarLabels)) != len(self.scalarLabels):
                raise ValueError("scalarLabels must not contain duplicates")
            for label in self.scalarLabels:
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", label):
                    raise ValueError(f"scalarLabels entry {label!r} is not a valid cell-set label")
        if self.coverMaterial != "GAS":
            # The native generator treats the cover material as the phase above
            # the surface. A cover equal to the implanted substrate voids the
            # target (measured: Si cover on Si gives P_total max = 0 in 2D/3D),
            # and no non-GAS cover has been proven to keep the target visible.
            raise ValueError(
                f"cellSet.coverMaterial {self.coverMaterial!r} is not supported for "
                "implant/anneal/analysis: the native DenseCellSet generator treats the cover "
                "material as the phase above the surface and skips cells of that material, so a "
                "cover equal to the implanted substrate voids the target and returns an all-zero "
                "field; use \"GAS\"")
        return self


class PearsonIVParametersV7(StrictModel):
    """The installed ``viennaps.PearsonIVParameters`` fields, in length units.

    ``mu``/``sigma`` use the project length unit (um); the native constructor
    stores exactly these values. ``beta`` and ``gamma`` are the fourth and third
    standardized moments the native ``PearsonIV`` function expects.
    """

    mu: Annotated[Number, Field(ge=0, le=10000)]
    sigma: Annotated[Number, Field(gt=0, le=10000)]
    beta: Annotated[Number, Field(gt=0, le=1000)]
    gamma: Annotated[Number, Field(ge=-1000, le=1000)]


class ManualImplantProfileV7(StrictModel):
    """A manually specified analytical profile.

    ViennaPS 4.7.0 ships no model database in the wheel, so the only proven
    profile source is an explicit moment set. ``kind`` selects the native
    constructor: ``ImplantPearsonIV``, ``ImplantDualPearsonIV`` or
    ``ImplantPearsonIVChanneling``. Moments are substrate-depth moments in the
    project length unit (um) and are *not* corrected for a screen oxide.
    """

    source: Literal["manual"]
    kind: Literal["pearsonIV", "dualPearsonIV", "pearsonIVChanneling"]
    head: PearsonIVParametersV7
    lateralMu: Number = 0
    lateralSigma: Annotated[Number, Field(gt=0, le=10000)] = 5
    tail: PearsonIVParametersV7 | None = None
    headFraction: Annotated[Number, Field(ge=0, le=1)] | None = None
    tailLateralMu: Number | None = None
    tailLateralSigma: Annotated[Number, Field(gt=0, le=10000)] | None = None
    tailFraction: Annotated[Number, Field(ge=0, le=1)] | None = None
    tailStartDepth: Annotated[Number, Field(ge=0, le=10000)] | None = None
    tailDecayLength: Annotated[Number, Field(gt=0, le=10000)] | None = None
    tailBlendWidth: Annotated[Number, Field(ge=0, le=10000)] = 0

    @model_validator(mode="after")
    def check_kind_fields(self) -> "ManualImplantProfileV7":
        dual_values = (self.tail, self.headFraction, self.tailLateralMu, self.tailLateralSigma)
        channeling_values = (self.tailFraction, self.tailStartDepth, self.tailDecayLength)
        # Validate values, not ``model_fields_set``: a project is re-parsed from
        # its own ``model_dump`` (which materialises defaults) before the worker
        # starts, so a harmless default such as tailBlendWidth=0 must stay valid.
        if self.kind == "pearsonIV":
            if any(value is not None for value in dual_values + channeling_values):
                raise ValueError("pearsonIV does not accept tail/channeling parameters; "
                                 "use kind=dualPearsonIV or pearsonIVChanneling")
            if float(self.tailBlendWidth) != 0.0:
                raise ValueError("tailBlendWidth applies only to pearsonIVChanneling")
        elif self.kind == "dualPearsonIV":
            missing = [name for name, value in zip(("tail", "headFraction", "tailLateralMu",
                                                    "tailLateralSigma"), dual_values) if value is None]
            if missing:
                raise ValueError("dualPearsonIV requires " + ", ".join(missing))
            if any(value is not None for value in channeling_values) or float(self.tailBlendWidth) != 0.0:
                raise ValueError("dualPearsonIV does not accept channeling parameters")
        else:
            missing = [name for name, value in zip(("tailFraction", "tailStartDepth",
                                                    "tailDecayLength"), channeling_values) if value is None]
            if missing:
                raise ValueError("pearsonIVChanneling requires " + ", ".join(missing))
            if any(value is not None for value in dual_values):
                raise ValueError("pearsonIVChanneling does not accept dual-Pearson parameters")
        return self


class ImplantDamageV7(StrictModel):
    """The installed ``viennaps.ImplantDamageHobler`` constructor arguments."""

    kind: Literal["hobler"]
    projectedRange: Annotated[Number, Field(gt=0, le=10000)]
    verticalSigma: Annotated[Number, Field(gt=0, le=10000)]
    lambdaValue: Annotated[Number, Field(gt=0, le=100000)]
    defectsPerIon: Annotated[Number, Field(ge=0, le=100000)]
    lateralSigma: Annotated[Number, Field(gt=0, le=10000)]
    lateralDeltaSigma: Annotated[Number, Field(ge=-10000, le=10000)] = 0


class ImplantLabelsV7(StrictModel):
    concentration: str | None = None
    damage: str | None = None
    lastDamage: str | None = None

    @model_validator(mode="after")
    def check_labels(self) -> "ImplantLabelsV7":
        for name in ("concentration", "damage", "lastDamage"):
            value = getattr(self, name)
            if value is None:
                continue
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", value):
                raise ValueError(f"{name} must be a valid cell-set label")
            error = reserved_label_error(value, f"labels.{name}")
            if error is not None:
                raise ValueError(error)
        return self


class ImplantStepV7(StrictModel):
    """Native ``viennaps.IonImplantation`` applied to the domain cell set.

    The model never moves the level sets; ``ps.Process(domain, implant, 0.0)``
    only stamps the concentration (and optional damage) fields into the cell
    set. ``energyKeV``/``rotationDeg`` are recorded request metadata: the
    manual Pearson IV path has no native energy or rotation parameter (only
    ``ImplantTableModel`` consumes them), so a nonzero rotation is refused
    instead of being silently dropped.
    """

    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    model: Literal["implant"]
    species: Annotated[str, Field(min_length=1, max_length=24, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
    material: Annotated[str, Field(min_length=1, max_length=64)]
    energyKeV: Annotated[Number, Field(gt=0, le=100000)]
    doseCm2: Annotated[Number, Field(gt=0, le=1e19)]
    tiltDeg: Annotated[Number, Field(ge=0, le=90)]
    rotationDeg: Annotated[Number, Field(ge=0, le=360)] = 0
    doseControl: Literal["Off", "WaferDose", "BeamDose"] = "WaferDose"
    profile: ManualImplantProfileV7
    damage: ImplantDamageV7 | None = None
    maskMaterials: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list)
    screenMaterials: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list)
    labels: ImplantLabelsV7 | None = None
    damageFactor: Annotated[Number, Field(gt=0, le=1000000)] = 1
    cellSet: StepCellSetV7

    @model_validator(mode="before")
    @classmethod
    def reject_unproven_sources(cls, data):
        if isinstance(data, dict):
            profile = data.get("profile")
            if isinstance(profile, dict) and profile.get("source") == "table":
                raise ValueError(
                    "implant profile.source=table is not exposed: the installed ViennaPS 4.7.0 wheel "
                    "ships no model database (viennaps.getModelDbRoot() is empty) and the workbench does "
                    "not bundle one, so a table lookup would fail at the native layer; use "
                    "profile.source=manual with explicit Pearson IV moments")
        return data

    @model_validator(mode="after")
    def check_implant_fields(self) -> "ImplantStepV7":
        if float(self.rotationDeg) != 0.0:
            raise ValueError(
                "rotationDeg is only consumed by the native ImplantTableModel; the manual Pearson IV "
                "path has no rotation parameter, so only rotationDeg=0 is accepted")
        for name in ("maskMaterials", "screenMaterials"):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must not contain duplicates")
        if set(self.maskMaterials) & set(self.screenMaterials):
            raise ValueError("maskMaterials and screenMaterials must be disjoint")
        if self.material in set(self.maskMaterials) | set(self.screenMaterials):
            raise ValueError("material must not also be a mask or screen material")
        resolved = self.labels or ImplantLabelsV7()
        stem = re.sub(r"[^A-Za-z0-9_]", "_", self.species)
        if not re.match(r"^[A-Za-z]", stem):
            stem = "dopant_" + stem
        concentration = resolved.concentration or f"{stem}_total"
        damage = resolved.damage
        last_damage = resolved.lastDamage
        if self.damage is None:
            if damage is not None or last_damage is not None:
                raise ValueError("labels.damage/lastDamage require a damage model")
        else:
            damage = damage or f"{stem}_damage"
            last_damage = last_damage or f"{damage}_last"
        if len({concentration, damage, last_damage} - {None}) != len(
                [value for value in (concentration, damage, last_damage) if value is not None]):
            raise ValueError("implant labels must be distinct")
        object.__setattr__(self, "labels", ImplantLabelsV7(
            concentration=concentration, damage=damage, lastDamage=last_damage))
        return self


class IsothermalScheduleV7(StrictModel):
    kind: Literal["isothermal"]
    temperatureK: Annotated[Number, Field(gt=0, le=5000)]
    durationSeconds: Annotated[Number, Field(ge=0, le=1000000)]


class StepScheduleV7(StrictModel):
    """``Anneal.setTemperatureSchedule(durations, temperatures)``.

    Native accepts N durations with N isothermal temperatures or N+1 ramp
    endpoint temperatures (the 4.7.0 example uses three ramp/soak steps).
    """

    kind: Literal["schedule"]
    durations: Annotated[list[Annotated[Number, Field(ge=0, le=1000000)]], Field(min_length=1, max_length=32)]
    temperatures: Annotated[list[Annotated[Number, Field(gt=0, le=5000)]], Field(min_length=1, max_length=33)]

    @model_validator(mode="after")
    def check_lengths(self) -> "StepScheduleV7":
        if len(self.temperatures) not in (len(self.durations), len(self.durations) + 1):
            raise ValueError(
                "schedule.temperatures must contain N isothermal values or N+1 ramp endpoints "
                f"for {len(self.durations)} duration(s)")
        return self


AnnealScheduleV7 = Annotated[Union[IsothermalScheduleV7, StepScheduleV7], Field(discriminator="kind")]


class ArrheniusDiffusivityV7(StrictModel):
    kind: Literal["arrhenius"]
    D0: Annotated[Number, Field(gt=0, le=1e40)]
    Ea_eV: Annotated[Number, Field(ge=0, le=100)]


class ConstantDiffusivityV7(StrictModel):
    kind: Literal["constant"]
    value: Annotated[Number, Field(ge=0, le=1e40)]


AnnealDiffusivityV7 = Annotated[Union[ArrheniusDiffusivityV7, ConstantDiffusivityV7], Field(discriminator="kind")]


class ImplicitSolverOptionsV7(StrictModel):
    maxIterations: Annotated[Integer, Field(ge=1, le=100000)] = 400
    relativeTolerance: Annotated[Number, Field(gt=0, le=1)] = 1e-6
    relaxation: Annotated[Number, Field(gt=0, le=2)] = 1.0


class SolidActivationV7(StrictModel):
    """``enableSolidActivation(True)`` + ``setSolidSolubilityArrhenius``.

    The native activation model writes ``C_active = C_SS·C/(C_SS+C)`` with
    ``C_SS = C0·exp(-Ea/kT)``. Without this block the anneal would diffuse the
    dopant but never produce an electrically active field.
    """

    solubilityC0: Annotated[Number, Field(gt=0, le=1e40)]
    solubilityEa_eV: Annotated[Number, Field(ge=0, le=100)]


class AnnealStepV7(StrictModel):
    """Native ``viennaps.Anneal`` on the cell set produced by an implant.

    Only the parameters proven to run on the installed 4.7.0 runtime are
    exposed. Defect coupling, TED/Fermi enhancement, trapping/segregation,
    clustering and source-field injection are intentionally absent: they are
    separate calibrated models whose parameter provenance the workbench cannot
    validate.
    """

    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    model: Literal["anneal"]
    mode: Literal["GaussSeidel", "Explicit"] = "GaussSeidel"
    schedule: AnnealScheduleV7
    diffusivity: AnnealDiffusivityV7
    timeStep: Annotated[Number, Field(gt=0, le=1000000)] | None = None
    stabilityFactor: Annotated[Number, Field(ge=0.01, le=1)] | None = None
    implicitSolver: ImplicitSolverOptionsV7 | None = None
    diffusionMaterials: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list)
    blockingMaterials: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list)
    speciesLabel: Annotated[str, Field(min_length=1, max_length=48)]
    activeLabel: Annotated[str, Field(min_length=1, max_length=48)]
    solidActivation: SolidActivationV7
    activationOnly: bool = False
    cellSet: StepCellSetV7

    @model_validator(mode="after")
    def check_anneal_fields(self) -> "AnnealStepV7":
        for name in ("speciesLabel", "activeLabel"):
            value = getattr(self, name)
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", value):
                raise ValueError(f"{name} must be a valid cell-set label")
            error = reserved_label_error(value, name)
            if error is not None:
                raise ValueError(error)
        if self.speciesLabel == self.activeLabel:
            raise ValueError("speciesLabel and activeLabel must be different cell-set fields")
        if set(self.diffusionMaterials) & set(self.blockingMaterials):
            raise ValueError("diffusionMaterials and blockingMaterials must be disjoint")
        if len(set(self.diffusionMaterials)) != len(self.diffusionMaterials):
            raise ValueError("diffusionMaterials must not contain duplicates")
        if len(set(self.blockingMaterials)) != len(self.blockingMaterials):
            raise ValueError("blockingMaterials must not contain duplicates")
        if self.mode == "Explicit" and self.implicitSolver is not None:
            raise ValueError("implicitSolver applies only to mode=GaussSeidel")
        return self


class NetDopingV7(StrictModel):
    """Native ``viennaps.NetDoping`` on the current cell set."""

    donorLabels: Annotated[list[Annotated[str, Field(min_length=1, max_length=48)]], Field(max_length=8)] = Field(default_factory=list)
    acceptorLabels: Annotated[list[Annotated[str, Field(min_length=1, max_length=48)]], Field(max_length=8)] = Field(default_factory=list)
    outputLabel: Annotated[str, Field(min_length=1, max_length=48)] = "net_doping"
    surfacePosition: Number | None = None

    @model_validator(mode="after")
    def check_net_doping(self) -> "NetDopingV7":
        if not self.donorLabels and not self.acceptorLabels:
            raise ValueError("netDoping requires at least one donor or acceptor label")
        for name in ("outputLabel", *self.donorLabels, *self.acceptorLabels):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", name):
                raise ValueError(f"{name!r} is not a valid cell-set label")
            error = reserved_label_error(name, "netDoping label")
            if error is not None:
                raise ValueError(error)
        labels = [*self.donorLabels, *self.acceptorLabels]
        if len(set(labels)) != len(labels):
            raise ValueError("netDoping labels must be unique across donors and acceptors")
        if self.outputLabel in labels:
            raise ValueError("netDoping.outputLabel must not overwrite an input field")
        return self


class SheetResistanceEntryV7(StrictModel):
    """One native ``viennaps.SheetResistance`` evaluation.

    ``computeElectron`` uses the Masetti-Severi n-type mobility (donors) and
    ``computeHole`` the p-type mobility (acceptors). The native length unit is
    fixed at 1e-4 cm (the workbench's um), which also sets the matching
    concentration unit 1e12 cm^-3.
    """

    concentrationLabel: Annotated[str, Field(min_length=1, max_length=48)]
    carrier: Literal["electron", "hole"]
    surfacePosition: Number | None = None

    @model_validator(mode="after")
    def check_label(self) -> "SheetResistanceEntryV7":
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", self.concentrationLabel):
            raise ValueError("concentrationLabel is not a valid cell-set label")
        error = reserved_label_error(self.concentrationLabel, "concentrationLabel")
        if error is not None:
            raise ValueError(error)
        return self


class AnalysisStepV7(StrictModel):
    """Native ``NetDoping`` / ``SheetResistance`` analysis of the cell set.

    This step never changes geometry. ``netDoping`` writes its output field into
    the cell set; ``sheetResistance`` only reports the native scalar.
    """

    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    model: Literal["analysis"]
    netDoping: NetDopingV7 | None = None
    sheetResistance: Annotated[list[SheetResistanceEntryV7], Field(max_length=8)] = Field(default_factory=list)
    scanDepth: Annotated[Number, Field(ge=0, le=10000)] | None = None
    cellSet: StepCellSetV7

    @model_validator(mode="after")
    def check_analysis_fields(self) -> "AnalysisStepV7":
        if self.netDoping is None and not self.sheetResistance:
            raise ValueError("analysis requires netDoping or at least one sheetResistance entry")
        if self.scanDepth is not None and self.netDoping is None:
            raise ValueError("scanDepth is the NetDoping lateral scan and requires netDoping")
        return self


class InsertBoxStepV7(StrictModel):
    """Insert a native ``viennals.Box`` level set as a new material.

    Reproduces the FinFET script's double-patterning mask::

        box = ps.LevelSet(domain.getGrid())
        ps.MakeGeometry(box, ps.Box(min, max)).apply()
        domain.insertNextLevelSetAsMaterial(box, material)

    ``min``/``max`` are absolute project coordinates in the declared length unit. The box is inserted
    above the current top level set, so the material order grows by one.
    """

    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    model: Literal["insertBox"]
    min: Vector3
    max: Vector3
    material: Annotated[str, Field(min_length=1, max_length=80)]

    @model_validator(mode="after")
    def check_box(self) -> "InsertBoxStepV7":
        if any(float(high) <= float(low) for low, high in zip(self.min, self.max)):
            raise ValueError("insertBox requires min < max on every axis")
        return self


class RemoveTopLevelSetStepV7(StrictModel):
    """Remove the last native level set (``domain.removeTopLevelSet()``)."""

    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    model: Literal["removeTopLevelSet"]


class PluginStepV7(StrictModel):
    """A process step owned by an installed process plugin.

    ``params`` is the plugin's canonical raw-step payload; the v2 dispatcher
    flattens it with ``id``/``name``/``enabled``/``model=pluginId`` before
    calling the plugin's ``parse_step`` hook.
    """

    id: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    model: Literal["plugin"]
    pluginId: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
    params: dict[str, Any] = Field(default_factory=dict)


StepV7 = Annotated[Union[StepV5, MultiParticleStepV7, SpecializedStepV7, GdsBooleanStepV7, CsvProcessStepV7,
                         ParticleShapeStepV7, ImplantStepV7, AnnealStepV7, AnalysisStepV7,
                         InsertBoxStepV7, RemoveTopLevelSetStepV7, PluginStepV7],
                   Field(discriminator="model")]


class DomainStateRef(StrictModel):
    formatVersion: Literal[1]
    stateId: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    structureHash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    dimension: Literal[2, 3]
    gridDelta: Annotated[Number, Field(gt=0)]
    xExtent: Annotated[Number, Field(gt=0)]
    yExtent: Annotated[Number, Field(gt=0)] | None = None
    boundary: Literal["reflective", "periodic"]
    boundaryConditions: Annotated[list[Literal["reflective", "periodic", "infinite"]], Field(min_length=2, max_length=3)]
    materialIdsByLevelSetIndex: Annotated[list[Integer], Field(min_length=1, max_length=64)]
    levelSetCount: Annotated[Integer, Field(ge=1, le=64)]
    viennapsVersion: Annotated[str, Field(min_length=1, max_length=32)]
    viennalsVersion: Annotated[str, Field(min_length=1, max_length=32)]
    sourceRunId: Annotated[str, Field(pattern=r"^[a-f0-9]{24}$")] | None = None
    sourceStepId: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")] | None = None

    @model_validator(mode="after")
    def state_shape(self):
        if (self.sourceRunId is None) != (self.sourceStepId is None):
            raise ValueError("sourceRunId and sourceStepId must both be present or both be omitted")
        if self.dimension == 2 and self.boundaryConditions != [self.boundary, "infinite"]:
            raise ValueError("2D native state boundary conditions must match the project and infinite height")
        if self.dimension == 3 and self.boundaryConditions != [self.boundary, self.boundary, "infinite"]:
            raise ValueError("3D native state boundary conditions must match the project lateral boundaries and infinite height")
        return self


def project_structure_hash(dimension: int, units: Units | UnitsLegacy, domain: Domain | Domain3D, geometry: dict) -> str:
    payload = {"dimension": dimension, "units": units.model_dump(mode="json"),
               "domain": domain.model_dump(mode="json", exclude_none=True), "geometry": geometry}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ProjectV6(StrictModel):
    schemaVersion: Literal[6]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    dimension: Literal[2, 3]
    units: UnitsLegacy
    domain: Domain | Domain3D
    geometry: dict
    steps: list[StepV5]
    solver: Solver
    view: View | None = None
    view3D: View3DV3 | None = None
    continuationState: DomainStateRef | None = None
    materialDefinitions: Annotated[list[MaterialDefinition], Field(max_length=64)] | None = None

    @model_validator(mode="before")
    @classmethod
    def select_dimension_domain(cls, data):
        return _select_dimension_domain(data, cls)

    @model_validator(mode="after")
    def version6_shape(self):
        if self.schemaVersion != 7 and self.units.length == "nm":
            raise ValueError("nanometre projects require schemaVersion 7")
        if self.dimension == 2 and (self.view is None or self.view3D is not None or not isinstance(self.domain, Domain)):
            raise ValueError("2D schemaVersion 6 requires 2D domain and view only")
        if self.dimension == 3 and (self.view3D is None or self.view is not None or not isinstance(self.domain, Domain3D)):
            raise ValueError("3D schemaVersion 6 requires 3D domain and view3D only")
        from pydantic import TypeAdapter
        geometry_adapter = GeometryV7 if self.schemaVersion == 7 else GeometryV3
        geometry = TypeAdapter(geometry_adapter).validate_python(self.geometry)
        normalized_geometry = geometry.model_dump(mode="json", exclude_none=True)
        validate_geometry_native_flags(normalized_geometry, self.dimension,
                                       _project_boundary_name(self.domain))
        if getattr(self.domain, "bounds", None) is not None and normalized_geometry.get("kind") != "plane":
            # Absolute native bounds are a deliberate escape hatch for the
            # plane-based FinFET fixture. The built-in/GDS geometry validators
            # reason in the centered [-extent/2, extent/2] frame, so accepting
            # bounds with them would validate the wrong coordinate frame.
            raise ValueError("domain.bounds is currently supported only with plane geometry")
        object.__setattr__(self, "geometry", normalized_geometry)
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step IDs must be unique")
        for index, step in enumerate(self.steps):
            if isinstance(step, DirectionalStepV5) and self.dimension == 2:
                for set_index, direction in _directional_rate_set_directions(step):
                    out_of_plane = normalized_out_of_plane_component(direction)
                    if out_of_plane > PLANAR_DIRECTION_TOLERANCE:
                        path = f"{index}" if set_index is None else f"{index}.rateSets[{set_index}]"
                        raise ValueError(
                            f"2D directional step {path} must use [x, h, 0]; its third direction component must be zero "
                            f"after normalization (raw {direction[2]!r} gives |z|={out_of_plane:.6g} on the unit vector)")
            if isinstance(step, BooleanStepV5):
                if self.dimension == 2 and (abs(step.position[2]) > 1e-12 or step.depth is not None):
                    raise ValueError(f"2D boolean step {index} requires position [x, h, 0] and no depth")
                if self.dimension == 3 and step.depth is None:
                    raise ValueError(f"3D boolean step {index} requires depth")
            if isinstance(step, CsvProcessStepV7) and self.dimension == 2:
                # ``ps.CSVFileProcess`` normalizes a 3-component direction in
                # both dimensions, but the 2D RateGrid/visibility path only has
                # an x/h plane; a nonzero third component has no native meaning.
                if abs(step.direction[2]) > 1e-12:
                    raise ValueError(f"2D csvProcess step {index} must use direction [x, h, 0]")
                # RateGrid::interpolateLinear/IDW use only offset[0] when D == 2.
                if abs(step.offset[1]) > 1e-12:
                    raise ValueError(f"2D csvProcess step {index} must use offset [x, 0]; the native 2D RateGrid has no second offset axis")
        state = self.continuationState
        if state is not None:
            boundary = self.domain.xBoundary if self.dimension == 2 else self.domain.boundaryXY
            if state.dimension != self.dimension or not math.isclose(state.gridDelta, self.domain.gridDelta, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError("continuation state dimension and gridDelta must match the project")
            if not math.isclose(state.xExtent, self.domain.xExtent, rel_tol=0.0, abs_tol=1e-12) or state.boundary != boundary:
                raise ValueError("continuation state x extent and boundary must match the project")
            if self.dimension == 2 and state.yExtent is not None:
                raise ValueError("2D continuation state must omit yExtent")
            if self.dimension == 3 and (state.yExtent is None or not math.isclose(state.yExtent, self.domain.yExtent, rel_tol=0.0, abs_tol=1e-12)):
                raise ValueError("3D continuation state y extent must match the project")
            if state.structureHash != project_structure_hash(self.dimension, self.units, self.domain, normalized_geometry):
                raise ValueError("continuation state geometry and domain fingerprint does not match the project")
            if len(state.materialIdsByLevelSetIndex) != state.levelSetCount:
                raise ValueError("continuation state material map length is invalid")
        return self


#: Scalar labels the installed viennacs 2.x DenseCellSet always writes.
#: ``Domain.generateCellSet``/``DenseCellSet.fromLevelSets`` add exactly these
#: two arrays; no broader label set exists in ViennaPS 4.6.2.
CELL_SET_NATIVE_SCALARS = ("Material", "FillingFraction")


class CellSetOutputV7(StrictModel):
    """Native ``viennaps.DenseCellSet`` output for every saved step.

    Native ``Domain.generateCellSet(position, coverMaterial, isAboveSurface)``
    takes an absolute *coordinate* and a direction flag, not a periodic flag.
    This request exposes the safe generic form only: a cell set built above the
    surface (``isAboveSurface=true``), i.e. the level-set band plus a cover
    region whose top is ``height`` above the highest surface point. The
    ``periodic`` flag mirrors the domain's lateral boundary and is applied to
    the cell set through ``DenseCellSet.setPeriodicBoundary``.
    """

    enabled: bool = False
    #: Distance from the highest surface point to the top of the cell set.
    #: Must span at least one grid interval: the installed viennacs crashes
    #: for a thinner band, so the native lower bound is enforced, not hidden.
    height: Number | None = None
    coverMaterial: Annotated[str, Field(min_length=1, max_length=64)] = "GAS"
    periodic: bool = False
    format: Literal["vtu", "json", "both"] = "both"
    #: Native scalar labels to summarise in the compact JSON document.
    #: Omitted means every label the native cell set exposes.
    scalarLabels: list[str] | None = None

    @model_validator(mode="after")
    def cell_set_shape(self):
        if self.enabled and self.height is None:
            raise ValueError("cellSet.height is required when cellSet.enabled is true")
        if self.height is not None and float(self.height) <= 0.0:
            raise ValueError("cellSet.height must be positive")
        if self.scalarLabels is not None:
            if not self.scalarLabels:
                raise ValueError("cellSet.scalarLabels must be omitted or a non-empty list")
            if len(set(self.scalarLabels)) != len(self.scalarLabels):
                raise ValueError("cellSet.scalarLabels must not contain duplicates")
            unknown = sorted(set(self.scalarLabels) - set(CELL_SET_NATIVE_SCALARS))
            if unknown:
                raise ValueError(
                    "cellSet.scalarLabels contains unsupported label(s) "
                    + ", ".join(repr(name) for name in unknown)
                    + "; ViennaPS 4.6.2 exposes Material and FillingFraction")
        return self


class OutputSettingsV7(StrictModel):
    """Native result artifacts; the surface mesh remains the viewer's required result."""

    levelSets: bool = False
    levelSetMeshes: bool = False
    surfaceMeshVtk: bool = False
    volumeMesh: bool = False
    #: Native ``Domain.saveVolumeMesh`` wrapping-layer epsilon. Omitted keeps
    #: the historical 0.01 um and stays out of the canonical request hash.
    volumeMeshDelta: Annotated[Number, Field(gt=0, le=1000)] | None = None
    nativeMetadata: bool = False
    cellSet: CellSetOutputV7 | None = None


class ProjectV7(ProjectV6):
    """Schema 7 is the unified extension point for advanced native capabilities."""

    schemaVersion: Literal[7]
    units: Units
    solver: SolverV7
    steps: list[StepV7]
    outputSettings: OutputSettingsV7 | None = None

    @model_validator(mode="after")
    def version7_geometry(self):
        geometry = self.geometry
        delta = self.domain.gridDelta
        if geometry["kind"] == "fin":
            if geometry["width"] < 2 * delta or geometry["height"] < 2 * delta:
                raise ValueError("fin width and height must span at least two grid intervals")
            if geometry["maskHeight"] > 0 and geometry["maskHeight"] < 2 * delta:
                raise ValueError("fin maskHeight must span at least two grid intervals or be zero")
            if self.dimension == 2 and geometry.get("extrusion") is not None:
                raise ValueError("Fin extrusion is only available for a 3D project")
            if geometry.get("extrusion") is not None:
                start, end = geometry["extrusion"]["extent"]
                if end - start < 2 * delta:
                    raise ValueError("fin extrusion extent must span at least two grid intervals")
                if min(abs(start), abs(end)) > self.domain.yExtent / 2 or max(abs(start), abs(end)) > self.domain.yExtent / 2:
                    raise ValueError("fin extrusion extent must fit inside the 3D y domain")
        elif geometry["kind"] == "gds":
            blur = geometry.get("blur")
            if blur is not None and blur.get("enabled", True):
                exposure_delta = (float(blur.get("delta"))
                                  if blur.get("delta") is not None else float(delta))
                if not math.isfinite(exposure_delta) or exposure_delta <= 0:
                    raise ValueError("geometry.blur.delta must be finite and positive")
                if exposure_delta > float(delta) + 1e-12:
                    raise ValueError(
                        "geometry.blur.delta is the exposure grid spacing and cannot be coarser than gridDelta")
                int(blur.get("gridRefine", 4))
                # The real exposure-grid budget depends on the GDS layout and
                # is enforced by backend.validation, which can read the asset.
                # Do not estimate it from the simulation domain here: a 2D
                # layout can be much taller than xExtent.
            if self.dimension == 2:
                if abs(geometry["position"][2]) > 1e-12:
                    raise ValueError("2D GDS position must use [x,h,0]")
                if any(row.get("baseHeight") is not None or row.get("height") is not None or row.get("mask") is not None for row in geometry["layers"]):
                    raise ValueError("2D GDS layer mappings do not accept 3D height or mask settings")
            else:
                for index, row in enumerate(geometry["layers"]):
                    if row.get("baseHeight") is None or row.get("height") is None:
                        raise ValueError(f"3D GDS layer mapping {index} requires baseHeight and height")
                    if row["height"] < 2 * delta:
                        raise ValueError(f"3D GDS layer mapping {index} height must span at least two grid intervals")
                    if abs(row["baseHeight"] + geometry["position"][2]) / delta > 1_000_000:
                        raise ValueError(f"3D GDS layer mapping {index} base height exceeds the application coordinate budget")
        elif geometry["kind"] == "stack" and geometry.get("gdsMask") is not None:
            mask = geometry["gdsMask"]
            if self.dimension != 3:
                raise ValueError("GDS mask transfer requires a 3D stack project")
            gds_layers = [row for row in geometry["layers"] if row.get("source") == "gds-mask"]
            if gds_layers:
                layer = gds_layers[0]
                if geometry["pattern"]["kind"] != "gds":
                    raise ValueError("a GDS mask layer requires top pattern gds")
                if layer["name"] != mask["name"] or layer["material"] != mask["material"] or abs(layer["thickness"] - mask["height"]) > 1e-9:
                    raise ValueError("GDS mask metadata must match its top stack layer")
            elif geometry["pattern"]["kind"] != "none":
                raise ValueError("legacy GDS masks cannot be combined with a built-in stack pattern")
            if mask["height"] < 2 * delta:
                raise ValueError("GDS mask height must span at least two grid intervals")
            if abs(mask["baseHeight"] + mask["position"][2]) / delta > 1_000_000:
                raise ValueError("GDS mask base height exceeds the application coordinate budget")
        for index, step in enumerate(self.steps):
            if isinstance(step, ParticleShapeStepV7) and step.shape == "box":
                # The native BoxDistribution binding always takes three axes;
                # 2D only consumes the first two. The project surface therefore
                # carries exactly the axes the dimension uses, so a 2D file can
                # never silently contain an unused third half-axis.
                if len(step.halfAxes) != self.dimension:
                    raise ValueError(
                        f"steps[{index}].halfAxes must contain {self.dimension} values "
                        f"({'[x, h]' if self.dimension == 2 else '[x, y, z]'}) for a {self.dimension}D project")
        for index, step in enumerate(self.steps):
            if not isinstance(step, GdsBooleanStepV7):
                continue
            # A GDS boolean operand is re-read from the geometry's own asset and
            # built as a real 3D body, so both a 3D project and GDS geometry are
            # required. Anything else must fail loudly instead of becoming a box.
            if self.dimension != 3:
                raise ValueError(f"steps[{index}]: gdsBoolean requires a 3D project")
            if geometry["kind"] != "gds":
                raise ValueError(f"steps[{index}]: gdsBoolean requires geometry.kind=gds; "
                                 "use the box boolean step for other structures")
            mask = step.gdsMask
            if float(mask.height) < 2 * delta:
                raise ValueError(f"steps[{index}].gdsMask.height must span at least two grid intervals")
            if abs(float(mask.baseHeight) + float(mask.position[2])) / delta > 1_000_000:
                raise ValueError(f"steps[{index}].gdsMask.baseHeight exceeds the application coordinate budget")
            if any(abs(float(value)) / delta > 1_000_000 for value in mask.position):
                raise ValueError(f"steps[{index}].gdsMask.position exceeds the application coordinate budget")
        for index, step in enumerate(self.steps):
            if isinstance(step, InsertBoxStepV7):
                if any(abs(float(value)) / delta > 1_000_000 for value in (*step.min, *step.max)):
                    raise ValueError(f"steps[{index}].insertBox coordinates exceed the application coordinate budget")
        if self.dimension == 2 and self.outputSettings is not None and self.outputSettings.volumeMesh:
            raise ValueError("native volume mesh output is only available for 3D projects")
        if (self.outputSettings is not None and self.outputSettings.volumeMeshDelta is not None
                and not self.outputSettings.volumeMesh):
            raise ValueError("outputSettings.volumeMeshDelta requires outputSettings.volumeMesh=true")
        if self.units.length == "um":
            # Field maxima are expressed in nanometres so a single schema can
            # admit both frames. Micrometre projects keep their historical
            # limits (and error surface) through this unit-aware check.
            delta = float(self.domain.gridDelta)
            if delta > 0.5:
                raise ValueError("domain.gridDelta must be <= 0.5 um for a micrometre project")
            if float(self.domain.xExtent) > 40:
                raise ValueError("domain.xExtent must be <= 40 um for a micrometre project")
            if self.dimension == 3 and float(self.domain.yExtent) > 40:
                raise ValueError("domain.yExtent must be <= 40 um for a micrometre project")
            bounds = getattr(self.domain, "bounds", None)
            if bounds is not None:
                xmin, xmax, ymin, ymax, _zmin, _zmax = (float(value) for value in bounds)
                if xmax - xmin > 40 or ymax - ymin > 40:
                    raise ValueError("domain.bounds span must stay within 40 um for a micrometre project")
            for index, step in enumerate(self.steps):
                if isinstance(step, ParticleShapeStepV7):
                    axes = [float(value) for value in (step.halfAxes or [])]
                    if step.radius is not None:
                        axes.append(float(step.radius))
                    if any(value > 2 for value in axes):
                        raise ValueError(f"steps[{index}]: particleShape magnitude must be <= 2 um for a micrometre project")
                if isinstance(step, BooleanStepV5):
                    if float(step.width) > 40 or float(step.height) > 100 or (
                            step.depth is not None and float(step.depth) > 40):
                        raise ValueError(f"steps[{index}]: boolean dimensions exceed the micrometre project limits")
            if (self.outputSettings is not None and self.outputSettings.volumeMeshDelta is not None
                    and float(self.outputSettings.volumeMeshDelta) > 1):
                raise ValueError("outputSettings.volumeMeshDelta must be <= 1 um for a micrometre project")
        cell_set = self.outputSettings.cellSet if self.outputSettings is not None else None
        if cell_set is not None and cell_set.enabled:
            delta = float(self.domain.gridDelta)
            height = float(cell_set.height)
            if height + 1e-12 < delta:
                raise ValueError(
                    f"cellSet.height must span at least one grid interval ({delta:g} um); "
                    "the installed viennacs DenseCellSet generator crashes on a thinner band")
            if height / delta > 1_000_000:
                raise ValueError("cellSet.height exceeds the application coordinate budget")
            domain_periodic = (self.domain.boundaryXY == "periodic" if self.dimension == 3
                               else self.domain.xBoundary == "periodic")
            if bool(cell_set.periodic) != domain_periodic:
                raise ValueError(
                    "cellSet.periodic must match the domain lateral boundary; the cell set is "
                    "generated from the domain's level sets and only the native neighbor topology "
                    "can be marked periodic")
            nx = max(1, math.ceil(float(self.domain.xExtent) / delta))
            if self.dimension == 3:
                ny = max(1, math.ceil(float(self.domain.yExtent) / delta))
                lateral = (nx + 2) * (ny + 2)
            else:
                lateral = nx + 2
            rows = math.ceil(height / delta) + 4
            estimate = lateral * max(rows, 2)
            if estimate > MAX_CELL_SET_CELLS:
                raise ValueError(
                    f"cellSet.height would create at least {estimate:,} cells, above the "
                    f"{MAX_CELL_SET_CELLS:,}-cell limit; reduce cellSet.height or coarsen gridDelta")
        delta = float(self.domain.gridDelta)
        domain_periodic = (self.domain.boundaryXY == "periodic" if self.dimension == 3
                           else self.domain.xBoundary == "periodic")
        nx = max(1, math.ceil(float(self.domain.xExtent) / delta))
        ny = (max(1, math.ceil(float(self.domain.yExtent) / delta))
              if self.dimension == 3 else 1)
        lateral = (nx + 2) * (ny + 2)
        for index, step in enumerate(self.steps):
            settings = getattr(step, "cellSet", None)
            if settings is None:
                continue
            height = float(settings.height)
            if height + 1e-12 < delta:
                raise ValueError(
                    f"steps[{index}].cellSet.height must span at least one grid interval "
                    f"({delta:g} um); the installed DenseCellSet generator crashes on a thinner band")
            if height / delta > 1_000_000:
                raise ValueError(f"steps[{index}].cellSet.height exceeds the application coordinate budget")
            if bool(settings.periodic) != domain_periodic:
                raise ValueError(
                    f"steps[{index}].cellSet.periodic must match the domain lateral boundary; the "
                    "cell set is generated from the domain's level sets and only the native "
                    "neighbor topology can be marked periodic")
            rows = math.ceil(height / delta) + 4
            estimate = lateral * max(rows, 2)
            if estimate > MAX_CELL_SET_CELLS:
                raise ValueError(
                    f"steps[{index}].cellSet.height would create at least {estimate:,} cells, above "
                    f"the {MAX_CELL_SET_CELLS:,}-cell limit; reduce the height or coarsen gridDelta")
            substrate_depth = settings.substrateDepth
            if substrate_depth is not None:
                if float(substrate_depth) + 1e-12 < 2 * delta:
                    raise ValueError(
                        f"steps[{index}].cellSet.substrateDepth must span at least two grid intervals "
                        f"({2 * delta:g} um) so the substrate has interior cells")
                rows = math.ceil((height + float(substrate_depth)) / delta) + 4
                estimate = lateral * max(rows, 2)
                if estimate > MAX_CELL_SET_CELLS:
                    raise ValueError(
                        f"steps[{index}].cellSet.height + substrateDepth would create at least "
                        f"{estimate:,} cells, above the {MAX_CELL_SET_CELLS:,}-cell limit; reduce the "
                        "depth or coarsen gridDelta")
        return self


class ProjectV5(StrictModel):
    schemaVersion: Literal[5]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    dimension: Literal[2, 3]
    units: UnitsLegacy
    domain: Domain | Domain3D
    geometry: dict
    steps: list[StepV5]
    solver: Solver
    view: View | None = None
    view3D: View3DV3 | None = None
    materialDefinitions: Annotated[list[MaterialDefinition], Field(max_length=64)] | None = None

    @model_validator(mode="before")
    @classmethod
    def select_dimension_domain(cls, data):
        return _select_dimension_domain(data, cls)

    @model_validator(mode="after")
    def version5_shape(self):
        if self.dimension == 2 and (self.view is None or self.view3D is not None or not isinstance(self.domain, Domain)):
            raise ValueError("2D schemaVersion 5 requires 2D domain and view only")
        if self.dimension == 3 and (self.view3D is None or self.view is not None or not isinstance(self.domain, Domain3D)):
            raise ValueError("3D schemaVersion 5 requires 3D domain and view3D only")
        from pydantic import TypeAdapter
        geometry = TypeAdapter(GeometryV3).validate_python(self.geometry)
        normalized_geometry = geometry.model_dump(mode="json", exclude_none=True)
        validate_geometry_native_flags(normalized_geometry, self.dimension,
                                       _project_boundary_name(self.domain))
        object.__setattr__(self, "geometry", normalized_geometry)
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step IDs must be unique")
        for index, step in enumerate(self.steps):
            if isinstance(step, DirectionalStepV5) and self.dimension == 2:
                for set_index, direction in _directional_rate_set_directions(step):
                    out_of_plane = normalized_out_of_plane_component(direction)
                    if out_of_plane > PLANAR_DIRECTION_TOLERANCE:
                        path = f"{index}" if set_index is None else f"{index}.rateSets[{set_index}]"
                        raise ValueError(
                            f"2D directional step {path} must use [x, h, 0]; its third direction component must be zero "
                            f"after normalization (raw {direction[2]!r} gives |z|={out_of_plane:.6g} on the unit vector)")
            elif isinstance(step, BooleanStepV5):
                if self.dimension == 2 and (abs(step.position[2]) > 1e-12 or step.depth is not None):
                    raise ValueError(f"2D boolean step {index} requires position [x, h, 0] and no depth")
                if self.dimension == 3 and step.depth is None:
                    raise ValueError(f"3D boolean step {index} requires depth")
        return self


ProjectAny = Annotated[Union[ProjectV1, ProjectV2, ProjectV3, ProjectV4, ProjectV5, ProjectV6, ProjectV7], Field(discriminator="schemaVersion")]


def parse_project(payload: object) -> ProjectV1 | ProjectV2 | ProjectV3 | ProjectV4 | ProjectV5 | ProjectV6 | ProjectV7:
    if isinstance(payload, dict) and type(payload.get("schemaVersion")) is int:
        if payload["schemaVersion"] == 1:
            return ProjectV1.model_validate(payload)
        if payload["schemaVersion"] == 2:
            return ProjectV2.model_validate(payload)
        if payload["schemaVersion"] == 3:
            return ProjectV3.model_validate(payload)
        if payload["schemaVersion"] == 4:
            return ProjectV4.model_validate(payload)
        if payload["schemaVersion"] == 5:
            return ProjectV5.model_validate(payload)
        if payload["schemaVersion"] == 6:
            return ProjectV6.model_validate(payload)
        if payload["schemaVersion"] == 7:
            return ProjectV7.model_validate(payload)
    from pydantic import TypeAdapter
    return TypeAdapter(ProjectAny).validate_python(payload)


class RunRequest(StrictModel):
    kind: Literal["preview", "simulate"]
    project: ProjectAny


class FluxRunRequest(StrictModel):
    project: ProjectV7
    stepId: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class ReplayStepRequirement(StrictModel):
    stepId: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    engine: Literal["CPU_DISK", "GPU_DISK", "CPU_TRIANGLE", "GPU_TRIANGLE", "not-applicable"]
    seed: Annotated[Integer, Field(ge=0, le=4294967295)] | None = None
    executionStrategy: Literal["symmetric-operator-splitting-v1", "native-multi-ion-v1"] | None = None
    fluxStrategy: Literal["per-species-native-mesh-merge-v1", "native-multi-ion-flux-v1"] | None = None

    @model_validator(mode="after")
    def engine_seed_pair(self) -> "ReplayStepRequirement":
        if self.engine != "not-applicable" and self.seed is None:
            raise ValueError(f"{self.engine} replay requirements need a seed")
        if self.engine == "not-applicable" and self.seed is not None:
            raise ValueError("non-particle steps must not carry a random seed")
        return self


class ReplayRequirements(StrictModel):
    viennapsVersion: str
    viennalsVersion: str
    cpuThreads: Annotated[Integer, Field(ge=1)]
    steps: list[ReplayStepRequirement]
    nativeEngineFingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    @field_validator("cpuThreads")
    @classmethod
    def cpu_threads_within_available_limit(cls, value: int) -> int:
        maximum = available_cpu_threads()
        if value > maximum:
            raise ValueError(f"must not exceed the {maximum} CPU threads available to this process")
        return value


class SliceRequestV7(StrictModel):
    # ``axis`` is the historical y-normal integer and is kept for byte-level
    # compatibility with saved requests/replays. New requests select the native
    # Slice.setSliceDimension index through ``sliceAxis`` (x=0, y=1, z=2).
    axis: Literal[1] = 1
    position: Number
    sliceAxis: Literal["x", "y", "z"] | None = None
    # Native Slice.setReflectX negates the first remaining coordinate. Omitted
    # means False so old request hashes stay unchanged.
    reflectX: bool | None = None


class ReplayProjectV1(StrictModel):
    format: Literal["viennaps-replay"]
    schemaVersion: Literal[1]
    normalizedProject: Project
    executionRequirements: ReplayRequirements


class ReplayProjectV2(StrictModel):
    format: Literal["viennaps-replay"]
    schemaVersion: Literal[2]
    normalizedProject: ProjectV2
    executionRequirements: ReplayRequirements


class ReplayProjectV3(StrictModel):
    format: Literal["viennaps-replay"]
    schemaVersion: Literal[3]
    normalizedProject: ProjectV3
    executionRequirements: ReplayRequirements


class ReplayProjectV4(StrictModel):
    format: Literal["viennaps-replay"]
    schemaVersion: Literal[4]
    normalizedProject: ProjectV4
    executionRequirements: ReplayRequirements


class ReplayProjectV5(StrictModel):
    format: Literal["viennaps-replay"]
    schemaVersion: Literal[5]
    normalizedProject: ProjectV5
    executionRequirements: ReplayRequirements


class ReplayProjectV6(StrictModel):
    format: Literal["viennaps-replay"]
    schemaVersion: Literal[6]
    replayMode: Literal["from-initial", "from-domain-state"]
    normalizedProject: ProjectV6
    executionRequirements: ReplayRequirements

    @model_validator(mode="after")
    def replay_mode_matches_project(self):
        expected = "from-domain-state" if self.normalizedProject.continuationState is not None else "from-initial"
        if self.replayMode != expected:
            raise ValueError("replayMode must identify whether the project begins from its initial structure or a saved Domain state")
        return self


class ReplayProjectV7(StrictModel):
    format: Literal["viennaps-replay"]
    schemaVersion: Literal[7]
    replayMode: Literal["from-initial", "from-domain-state"]
    analysisMode: Literal["process", "flux", "slice"] = "process"
    analysisStepId: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")] | None = None
    sliceRequest: SliceRequestV7 | None = None
    normalizedProject: ProjectV7
    executionRequirements: ReplayRequirements

    @model_validator(mode="after")
    def replay_modes_match_project(self):
        expected = "from-domain-state" if self.normalizedProject.continuationState is not None else "from-initial"
        if self.replayMode != expected:
            raise ValueError("replayMode must identify whether the project begins from its initial structure or a saved Domain state")
        if (self.analysisMode == "flux") != (self.analysisStepId is not None):
            raise ValueError("flux replay requires exactly one analysisStepId")
        if (self.analysisMode == "slice") != (self.sliceRequest is not None):
            raise ValueError("slice replay requires exactly one sliceRequest")
        if self.analysisMode == "slice" and (self.normalizedProject.dimension != 3 or self.normalizedProject.steps):
            raise ValueError("native Slice replay requires a 3D geometry project without process steps")
        return self


ReplayProject = ReplayProjectV1


def parse_replay(payload: object) -> ReplayProjectV1 | ReplayProjectV2 | ReplayProjectV3 | ReplayProjectV4 | ReplayProjectV5 | ReplayProjectV6 | ReplayProjectV7:
    from pydantic import TypeAdapter
    return TypeAdapter(Annotated[Union[ReplayProjectV1, ReplayProjectV2, ReplayProjectV3, ReplayProjectV4, ReplayProjectV5, ReplayProjectV6, ReplayProjectV7], Field(discriminator="schemaVersion")]).validate_python(payload)


def model_error_path(message: str) -> str:
    """Extract a stable field path from common Pydantic messages when possible."""
    match = re.search(r"(?:steps\s*\[?\s*\d+\]?|domain|geometry|solver|view|units)", message)
    return match.group(0).replace(" ", "") if match else "$"
