"""Engine-neutral job protocol shared by every simulation engine.

The workbench compiler emits :class:`StepIR` objects. An engine runner consumes a
:class:`JobSpec` (initial structure + StepIR sequence + render/benchmark request)
and emits up to one :class:`Frame` per step plus a :class:`StepOutcome` per step.
Nothing in this module imports viennaps, viennals, spx, numpy or the backend: it is
the only contract every engine interpreter agrees on (including SPX-private Python
3.11 and external custom-model environments).

Unit convention (deliberate, documented in docs/engine-abstraction.md):

* length  -> micrometre (``um``)
* time    -> minute (``min``) for every rate and duration
* z axis  -> up; origin at the substrate top surface, lateral window local [0..W, 0..D]
* removal -> negative rate, deposition -> positive rate

The workbench project layer may use nm or seconds; the compiler converts. Engines
never see project units.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = 1
LENGTH_UNIT = "um"
TIME_UNIT = "min"

StepKind = Literal["initial", "deposit", "etch", "planarize", "mask", "strip"]
Mechanism = Literal[
    "initial", "isotropic", "anisotropic", "cmp", "gds_mask",
    "strip", "fill", "ellipsoidal", "litho", "custom",
]
Comparability = Literal["exact", "approximate", "no_counterpart"]
StepStatus = Literal["ok", "out_of_domain", "unsupported", "failed", "skipped"]
FrameKind = Literal["mesh", "raster"]
SolveMode = Literal["stepwise", "batch_route"]


class ProtocolError(ValueError):
    """Raised when a job spec or an engine response violates this contract."""


@dataclass(frozen=True)
class DomainSpec:
    """Computational domain. ``grid_delta`` is advisory for analytic engines."""

    width: float
    depth: float
    height: float
    grid_delta: float
    dimensions: int = 3
    # Lower-left corner of the window in the *engine's* global frame (um).
    # StepIR coordinates stay structure-local; an engine whose structures are
    # placed on a layout (SPX) adds this offset when it is asked for a point.
    origin_xy: list[float] | None = None

    def validate(self) -> None:
        for name in ("width", "depth", "height", "grid_delta"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value <= 0:
                raise ProtocolError(f"domain.{name} must be a positive number, got {value!r}")
        if self.dimensions not in (2, 3):
            raise ProtocolError("domain.dimensions must be 2 or 3")


@dataclass(frozen=True)
class Film:
    """One layer of the initial material stack, bottom-up after the substrate."""

    material: str
    thickness: float


@dataclass(frozen=True)
class InitialStructure:
    """Where all engines start."""

    kind: Literal["plane", "trench"]
    substrate: str
    substrate_thickness: float
    films: tuple[Film, ...] = ()
    trench_width: float | None = None
    trench_depth: float | None = None
    mask_material: str | None = None

    def validate(self) -> None:
        if self.kind not in ("plane", "trench"):
            raise ProtocolError(f"unsupported initial structure kind {self.kind!r}")
        if self.substrate_thickness <= 0:
            raise ProtocolError("substrate thickness must be positive")
        for film in self.films:
            if film.thickness <= 0:
                raise ProtocolError(f"film {film.material!r} thickness must be positive")
        if self.kind == "trench":
            if not self.trench_width or not self.trench_depth:
                raise ProtocolError("trench needs trench_width and trench_depth")
            if self.mask_material is None:
                raise ProtocolError("trench needs mask_material")


@dataclass(frozen=True)
class StepIR:
    """One engine-neutral process step.

    ``rates`` maps canonical material name -> signed rate in um/min (removal negative).
    A material absent from ``rates`` is untouched (selectivity 0).

    ``model`` is an optional process-specific model override (for example
    ``"damascene_cmp_dishing"`` or ``"high_ar_contact_etch"``). Reference engines
    such as SPX ignore ``model`` and lower ``(kind, mechanism)``; custom model
    engines dispatch to ``model`` first when registered, allowing per-step model
    experimentation on the exact same StepIR flow.
    """

    step_id: str
    kind: StepKind
    mechanism: Mechanism
    duration: float = 0.0
    rates: dict[str, float] = field(default_factory=dict)
    anisotropy: float = 1.0
    comparability: Comparability = "exact"
    parameters: dict[str, Any] = field(default_factory=dict)
    model: str | None = None

    def validate(self) -> None:
        if not self.step_id:
            raise ProtocolError("step_id is required")
        if self.kind not in ("initial", "deposit", "etch", "planarize", "mask", "strip"):
            raise ProtocolError(f"unsupported step kind {self.kind!r}")
        if self.mechanism not in ("initial", "isotropic", "anisotropic", "cmp",
                                  "gds_mask", "strip", "fill", "ellipsoidal", "litho",
                                  "custom"):
            raise ProtocolError(f"unsupported mechanism {self.mechanism!r}")
        if self.mechanism == "custom" and not self.model:
            raise ProtocolError(f"custom step {self.step_id!r} requires a model name")
        if self.mechanism == "strip" and not self.parameters.get("materials"):
            raise ProtocolError(f"strip step {self.step_id!r} needs parameters.materials")
        if self.mechanism == "fill" and not self.parameters.get("material"):
            raise ProtocolError(f"fill step {self.step_id!r} needs parameters.material")
        if self.mechanism == "litho":
            for key in ("layer", "thickness"):
                if key not in self.parameters:
                    raise ProtocolError(f"litho step {self.step_id!r} needs parameters.{key}")
        if self.comparability not in ("exact", "approximate", "no_counterpart"):
            raise ProtocolError(f"unsupported comparability {self.comparability!r}")
        if (self.kind in ("deposit", "etch")
                and self.mechanism not in ("fill", "strip", "custom")
                and self.duration <= 0):
            raise ProtocolError(f"step {self.step_id!r} needs a positive duration")
        for material, rate in self.rates.items():
            if not isinstance(rate, (int, float)) or isinstance(rate, bool):
                raise ProtocolError(f"rate for {material!r} must be numeric, got {rate!r}")


@dataclass(frozen=True)
class ProbeSpec:
    """Points every engine must answer with a canonical material label."""

    points: list[list[float]]

    def validate(self) -> None:
        for point in self.points:
            if len(point) != 3 or not all(isinstance(value, (int, float)) for value in point):
                raise ProtocolError(f"probe point {point!r} must be three numbers")


@dataclass(frozen=True)
class SectionSpec:
    """A cross-section every engine must report interface heights on."""

    columns: list[list[float]]
    materials: list[str]
    z_min: float
    z_max: float

    def validate(self) -> None:
        if not self.columns or not self.materials:
            raise ProtocolError("section needs columns and materials")
        if self.z_min >= self.z_max:
            raise ProtocolError("section z_min must be below z_max")
        for column in self.columns:
            if len(column) != 2 or not all(isinstance(v, (int, float)) for v in column):
                raise ProtocolError(f"section column {column!r} must be two numbers")


@dataclass(frozen=True)
class RenderSpec:
    """Render request."""

    width: int = 900
    height: int = 700
    view: str = "iso"
    camera: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JobSpec:
    """Everything a runner needs; serialised as one JSON file."""

    case_id: str
    engine: str
    domain: DomainSpec
    initial: InitialStructure
    steps: tuple[StepIR, ...]
    render: RenderSpec = field(default_factory=RenderSpec)
    probe: ProbeSpec | None = None
    section: SectionSpec | None = None
    layout: dict[str, Any] | None = None
    restore: str | None = None
    emit_frames: bool = True
    emit_checkpoints: bool = True
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ProtocolError(
                f"job schemaVersion {self.schema_version} != {SCHEMA_VERSION}")
        if not self.case_id:
            raise ProtocolError("case_id is required")
        self.domain.validate()
        self.initial.validate()
        if not self.steps:
            raise ProtocolError("job needs at least one step")
        if self.probe is not None:
            self.probe.validate()
        if self.section is not None:
            self.section.validate()
        seen: set[str] = set()
        for step in self.steps:
            step.validate()
            if step.step_id in seen:
                raise ProtocolError(f"duplicate step_id {step.step_id!r}")
            seen.add(step.step_id)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "JobSpec":
        domain = DomainSpec(**payload["domain"])
        raw_initial = payload["initial"]
        initial = InitialStructure(
            kind=raw_initial["kind"],
            substrate=raw_initial["substrate"],
            substrate_thickness=float(raw_initial["substrate_thickness"]),
            films=tuple(Film(material=f["material"], thickness=float(f["thickness"]))
                        for f in raw_initial.get("films", [])),
            trench_width=raw_initial.get("trench_width"),
            trench_depth=raw_initial.get("trench_depth"),
            mask_material=raw_initial.get("mask_material"),
        )
        steps = tuple(StepIR(
            step_id=s["step_id"],
            kind=s["kind"],
            mechanism=s["mechanism"],
            duration=float(s.get("duration", 0.0)),
            rates=_rates(s.get("rates") or {}, s.get("step_id", "?")),
            anisotropy=float(s.get("anisotropy", 1.0)),
            comparability=s.get("comparability", "exact"),
            parameters=dict(s.get("parameters") or {}),
            model=(str(s["model"]) if s.get("model") else None),
        ) for s in payload["steps"])
        job = cls(
            case_id=payload["case_id"],
            engine=payload.get("engine", "any"),
            domain=domain,
            initial=initial,
            steps=steps,
            render=RenderSpec(**payload.get("render", {})),
            probe=(ProbeSpec(points=[[float(v) for v in p]
                                     for p in payload["probe"]["points"]])
                   if payload.get("probe") else None),
            section=(SectionSpec(
                columns=[[float(v) for v in c]
                         for c in payload["section"]["columns"]],
                materials=[str(m) for m in payload["section"]["materials"]],
                z_min=float(payload["section"]["z_min"]),
                z_max=float(payload["section"]["z_max"]))
                if payload.get("section") else None),
            restore=(str(payload["restore"]) if payload.get("restore") else None),
            layout=(dict(payload["layout"]) if payload.get("layout") else None),
            emit_frames=bool(payload.get("emit_frames", True)),
            emit_checkpoints=bool(payload.get("emit_checkpoints", True)),
            schema_version=int(payload.get("schema_version", SCHEMA_VERSION)),
        )
        job.validate()
        return job


def _rates(raw: dict[str, Any], step_id: str) -> dict[str, float]:
    rates: dict[str, float] = {}
    for material, value in raw.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ProtocolError(
                f"rate for {material!r} in step {step_id!r} must be numeric, got {value!r}")
        rates[str(material)] = float(value)
    return rates


def flow_materials(job: JobSpec, *, include_masks: bool = False) -> list[str]:
    """Ordered unique canonical materials introduced across initial + all steps."""
    seen: list[str] = []
    mask_names = {"PMMA", "Mask"}

    def add(name: Any) -> None:
        if not name:
            return
        text = str(name)
        if not include_masks and text in mask_names:
            return
        if text not in seen:
            seen.append(text)

    add(job.initial.substrate)
    for film in job.initial.films:
        add(film.material)
    if include_masks and job.initial.mask_material:
        add(job.initial.mask_material)
    for step in job.steps:
        for mat, rate in step.rates.items():
            if float(rate) != 0.0:
                add(mat)
        params = step.parameters or {}
        if step.mechanism == "fill":
            add(params.get("material"))
        elif step.mechanism == "litho" and include_masks:
            add(params.get("material", "PMMA"))
        elif step.mechanism == "cmp":
            add(params.get("stop_material"))
        elif step.mechanism == "ellipsoidal":
            for mat in (params.get("rates") or {}):
                add(mat)
    return seen


@dataclass(frozen=True)
class Frame:
    """One renderable frame. ``mesh`` and ``raster`` are mutually exclusive."""

    step_id: str
    kind: FrameKind
    path: str
    width: int = 0
    height: int = 0
    camera: dict[str, Any] = field(default_factory=dict)
    materials: list[dict[str, Any]] = field(default_factory=list)
    bbox: list[float] = field(default_factory=list)

    def validate(self) -> None:
        if self.kind not in ("mesh", "raster"):
            raise ProtocolError(f"unsupported frame kind {self.kind!r}")
        if not self.path:
            raise ProtocolError("frame path is required")


@dataclass(frozen=True)
class StepOutcome:
    """Per-step result.

    Timing fields are strictly separated so solver speed benchmarks are never
    polluted by screenshot rendering, mesh export, or probe IPC:

    * ``solve_ms``      - pure physics/geometry step update (``None`` for batch-route
                          engines such as SPX whose solver runs all steps in one call);
    * ``frame_ms``      - mesh extraction or raster screenshot time;
    * ``probe_ms``      - probe-lattice query time;
    * ``section_ms``    - cross-section height query time;
    * ``checkpoint_ms`` - state serialisation time;
    * ``wall_ms``       - backward-compatible primary step wall time (equals
                          ``solve_ms`` when available, otherwise ``frame_ms``).
    """

    step_id: str
    status: StepStatus
    wall_ms: float
    solve_ms: float | None = None
    frame_ms: float = 0.0
    probe_ms: float = 0.0
    section_ms: float = 0.0
    checkpoint_ms: float = 0.0
    frame: Frame | None = None
    checkpoint: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    probe: list[str] | None = None
    section: dict[str, list[float | None]] | None = None


@dataclass(frozen=True)
class EngineInfo:
    """Self-declaration emitted by an engine inside ``result.json``."""

    engine_id: str
    version: str
    dimensions: tuple[int, ...]
    mechanisms: tuple[str, ...]
    frame_kinds: tuple[str, ...]
    adapter: str
    solve_mode: SolveMode = "stepwise"
    probe_method: str = "unknown"
    limits: dict[str, Any] = field(default_factory=dict)

    def supports(self, step: StepIR) -> bool:
        return step.mechanism in self.mechanisms


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(path)
