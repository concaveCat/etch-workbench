"""Conservative artifact-footprint preflight for a normalized project.

The supervisor and worker enforce a hard per-run artifact cap
(``backend.runtime_limits.artifact_limit_bytes``, default 4096 MiB, configured
with ``WORKBENCH_MAX_ARTIFACT_MIB``). A long run that only reaches that cap
while its final snapshot is materialized is expensive to discover: the user
waits for the simulation and then gets a 413 from the download route or a
failed snapshot read.

This module estimates that footprint *before* the worker starts. It is a
heuristic, not a measurement. The coefficients were fitted to completed runs in
the local workbench (read-only manifests):

===========================  ==================  ========  =============
domain/grid                  enabled steps       on disk   primary est.
===========================  ==================  ========  =============
1 x 1 um, 0.003 um, GDS      13                  599 MiB   684 MiB
1 x 1 um, 0.004 um, GDS      13                  387 MiB   385 MiB
1 x 1 um, 0.004 um, GDS      11                  244 MiB   320 MiB
1 x 1 um, 0.004 um, GDS      2                    75 MiB    91 MiB
1 x 1 um, 0.002 um, GDS      preview only        139 MiB   132 MiB
5 x 5 um, 0.05 um, 3D fin    7 + volume mesh     108 MiB   166 MiB
===========================  ==================  ========  =============

The estimate models the two contributors that dominate a ViennaPS run:

* one native ``state-<step>.vpsd`` per executed step plus the initial state.
  Its size grows with the lateral grid-cell count (``x*y/gridDelta^2`` in 3D,
  ``x/gridDelta`` in 2D) and with the step index, because every process step
  adds a level set / surface;
* the surface snapshot JSON. The initial snapshot is eager; later snapshots
  are deferred by default (``VIENNAPS_WORKBENCH_LAZY_SNAPSHOTS``) and are
  materialized on first read. The preflight includes the initial snapshot and
  the largest step snapshot, because opening the viewer or the run page reads
  them. Full-snapshot materialization is reported separately as
  ``archiveEstimateBytes`` so a download that would be refused with 413 is
  visible in advance.

Geometry factors are deliberately coarse. GDS is calibrated directly. A fin /
stack / trench / hole / plane is scaled down for native state size (the actual
surface is a small feature inside a large lateral domain) but kept near GDS for
the snapshot mesh (the extracted mesh still spans the full lateral domain).

Every result carries a safety factor and a confidence. A configuration with an
output mode that has no calibration data (``levelSets`` / ``levelSetMeshes``)
is reported as low confidence and is *not* rejected, only warned about, so an
uncertain estimate cannot block a run.
"""
from __future__ import annotations

import math
import os
from typing import Any

from .runtime_limits import MAX_CELL_SET_CELLS, artifact_limit_bytes

MIB = 1024 * 1024

#: Allow the run, but tell the user it is in the upper quarter of the cap.
WARNING_FRACTION = 0.75
#: Reject before the worker starts above this fraction of the cap.
REJECT_FRACTION = 1.0
#: Multiplier applied to the raw fit. The local calibration residuals are
#: within roughly +/-20 %; this keeps a borderline run on the safe side.
SAFETY_FACTOR = 1.20


def __getattr__(name: str) -> Any:
    # Backwards compatibility for callers that read the old module constant.
    # Resolve it from the environment on every access instead of freezing the
    # value at import time.
    if name == "ARTIFACT_CAP_BYTES":
        return artifact_limit_bytes()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Bytes per lateral grid cell for the native state of step i:
#   state_i = cells * state_geometry_factor * (160 + 18 * i)
# Summed over the initial state and every enabled step this reproduces the
# 0.003 um (448 MiB of states) and 0.004 um (247 MiB) FinFET runs within 4 %.
STATE_BASE_BYTES_PER_CELL = 160
STATE_GROWTH_BYTES_PER_CELL = 18
# Snapshot JSON: initial = 300 cells, step i adds 75 cells. The final snapshot
# of the 13-step runs is 1190-1300 bytes/cell.
INITIAL_SNAPSHOT_BYTES_PER_CELL = 300
STEP_SNAPSHOT_BYTES_PER_CELL = 300
STEP_SNAPSHOT_GROWTH_BYTES_PER_CELL = 75
# Optional outputs. Volume meshes were calibrated on 5 x 5 um / 0.05 um fin
# runs (~500-600 bytes per lateral cell per step). Level-set exports have no
# local calibration and are intentionally generous.
VOLUME_MESH_BYTES_PER_CELL = 600
LEVEL_SET_BYTES_PER_CELL = 600
LEVEL_SET_MESH_BYTES_PER_CELL = 300
NATIVE_METADATA_BYTES_PER_CELL = 32
#: Native DenseCellSet VTU payload measured on plane/trench probes at
#: 2D and 3D: 19.8-20.9 bytes per cell (zlib-compressed appended arrays),
#: plus a fixed XML header. 24 B/cell is a small safety margin. The compact
#: JSON document is a summary (statistics, not per-cell arrays) and is
#: independent of the cell count.
CELL_SET_VTU_BYTES_PER_CELL = 24
CELL_SET_JSON_BYTES = 64 * 1024

#: ``state-*.vpsd`` growth per geometry kind. GDS is the calibrated reference.
STATE_GEOMETRY_FACTOR = {
    "gds": 1.0,
    "fin": 0.28,
    "stack": 0.5,
    "trench": 0.35,
    "hole": 0.35,
    "plane": 0.25,
}
#: Snapshot mesh growth per geometry kind. Fin meshes are stored at full
#: float precision and often cover the whole lateral substrate.
SNAPSHOT_GEOMETRY_FACTOR = {
    "gds": 1.0,
    "fin": 2.0,
    "stack": 1.2,
    "trench": 1.2,
    "hole": 1.2,
    "plane": 1.0,
}
GEOMETRY_ONLY_MODELS = {"removeMaterial", "planarize", "boolean", "particleShape"}
MODEL_WEIGHTS = {
    "isotropic": 0.9,
    "singleParticle": 1.0,
    "directional": 1.0,
    "multiParticle": 1.0,
}

CALIBRATION_RUNS = [
    {"shape": "3D GDS 1x1 um, gridDelta 0.003, 13 steps", "onDiskMiB": 599, "estimateMiB": 684},
    {"shape": "3D GDS 1x1 um, gridDelta 0.004, 13 steps", "onDiskMiB": 387, "estimateMiB": 385},
    {"shape": "3D GDS 1x1 um, gridDelta 0.004, 11 steps", "onDiskMiB": 244, "estimateMiB": 320},
    {"shape": "3D GDS 1x1 um, gridDelta 0.004, 2 steps", "onDiskMiB": 75, "estimateMiB": 91},
    {"shape": "3D GDS 1x1 um, gridDelta 0.002, preview only", "onDiskMiB": 139, "estimateMiB": 132},
    {"shape": "3D fin 5x5 um, gridDelta 0.05, 7 steps + volume mesh", "onDiskMiB": 108, "estimateMiB": 166},
]


def lazy_snapshots_enabled() -> bool:
    """Match ``worker.simulate.LAZY_SNAPSHOTS`` without importing the worker."""
    return os.environ.get("VIENNAPS_WORKBENCH_LAZY_SNAPSHOTS") != "0"


def _enabled_steps(project: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in project.get("steps") or [] if step.get("enabled")]


def _step_weight(step: dict[str, Any]) -> float:
    model = step.get("model")
    if model in GEOMETRY_ONLY_MODELS:
        return 0.85
    if model == "specialized":
        # Flux-driven ALD/etch adds a surface; native-only models change less.
        return 1.0 if step.get("nativeParameters") is not None else 0.9
    return MODEL_WEIGHTS.get(str(model), 1.0)


def _lateral_cells(project: dict[str, Any]) -> float:
    domain = project.get("domain") or {}
    try:
        delta = float(domain.get("gridDelta") or 0.0)
        extent_x = float(domain.get("xExtent") or 0.0)
    except (TypeError, ValueError):
        return 1.0
    if delta <= 0.0:
        return 1.0
    if int(project.get("dimension") or 2) == 3:
        try:
            extent_y = float(domain.get("yExtent") or 0.0)
        except (TypeError, ValueError):
            extent_y = 0.0
        return max(1.0, extent_x * extent_y / (delta * delta))
    return max(1.0, extent_x / delta)


def _geometry_kind(project: dict[str, Any]) -> str:
    geometry = project.get("geometry")
    if isinstance(geometry, dict) and isinstance(geometry.get("kind"), str):
        return geometry["kind"]
    return "plane"


def _levelset_count(project: dict[str, Any]) -> int:
    """Best-effort level-set count from the initial geometry."""
    geometry = project.get("geometry")
    if not isinstance(geometry, dict):
        return 1
    if geometry.get("kind") == "gds":
        return max(1, len(geometry.get("layers") or []) + 1)
    if geometry.get("kind") == "stack":
        return max(1, len(geometry.get("layers") or []) + 1)
    return 1


def _layer_growth(project: dict[str, Any]) -> float:
    """Mild growth for many mapped GDS / stack layers.

    The calibrated FinFET projects have four mapped layers and eleven level
    sets. More layers mean more native level sets, but the relationship is not
    linear in this model, so this is capped at 1.5x.
    """
    geometry = project.get("geometry")
    if not isinstance(geometry, dict):
        return 1.0
    if geometry.get("kind") not in {"gds", "stack"}:
        return 1.0
    layers = len(geometry.get("layers") or [])
    return min(1.5, 1.0 + max(0, layers - 8) * 0.05)


def _all_snapshot_bytes(cells: float, snapshot_factor: float, layer_factor: float,
                        step_count: int) -> int:
    total = 0.0
    for index in range(max(1, step_count)):
        total += STEP_SNAPSHOT_BYTES_PER_CELL + STEP_SNAPSHOT_GROWTH_BYTES_PER_CELL * index
    return int(round(cells * snapshot_factor * layer_factor * total))


def _lateral_grid(project: dict[str, Any]) -> tuple[float, float]:
    """Lateral level-set cell counts (float, unrounded) of the domain."""
    domain = project.get("domain") or {}
    try:
        delta = float(domain.get("gridDelta") or 0.0)
        x_extent = float(domain.get("xExtent") or 0.0)
        y_extent = float(domain.get("yExtent") or 0.0) if int(project.get("dimension") or 2) == 3 else 0.0
    except (TypeError, ValueError):
        return 1.0, 1.0
    if delta <= 0.0:
        return 1.0, 1.0
    return max(1.0, x_extent / delta), max(1.0, y_extent / delta)


def _structure_vertical_extent(project: dict[str, Any]) -> float:
    """Conservative vertical span of the initial geometry in um.

    Used only to size a requested DenseCellSet; the worker repeats the bound
    from the measured native surface before it builds anything. GDS is the one
    kind where the initial depth is not fully described by the request, so its
    layers are used when they carry 3D heights and zero otherwise.
    """
    geometry = project.get("geometry")
    if not isinstance(geometry, dict):
        return 0.0

    def number(key: str) -> float:
        try:
            return abs(float(geometry.get(key) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    kind = geometry.get("kind")
    if kind == "plane":
        # ``height`` is the absolute plane elevation, not a thickness. The cell
        # set starts from the level-set band around that plane, so translation
        # must not change its vertical span.
        return 0.0
    if kind == "trench":
        return number("depth") + number("maskHeight")
    if kind == "hole":
        return number("depth") + number("maskHeight")
    if kind == "fin":
        return number("height") + number("maskHeight")
    if kind == "stack":
        # ``baseHeight`` is an absolute offset; only the layer thicknesses add
        # vertical span above the substrate.
        return sum(abs(float(row.get("thickness") or 0.0)) for row in geometry.get("layers") or [])
    if kind == "gds":
        heights: list[float] = []
        for row in geometry.get("layers") or []:
            if row.get("baseHeight") is None or row.get("height") is None:
                continue
            base = float(row["baseHeight"])
            heights.extend((base, base + abs(float(row["height"]))))
        return max(heights) - min(heights) if heights else 0.0
    return 0.0


def _cell_set_estimate(project: dict[str, Any], *, step_count: int) -> tuple[int, int]:
    """Return ``(bytes, cells_upper_bound)`` for the optional cell set."""
    settings = project.get("outputSettings") or {}
    cell_set = settings.get("cellSet")
    if not isinstance(cell_set, dict) or not cell_set.get("enabled"):
        return 0, 0
    domain = project.get("domain") or {}
    try:
        delta = float(domain.get("gridDelta") or 0.0)
        height = max(0.0, float(cell_set.get("height") or 0.0))
    except (TypeError, ValueError):
        return 0, 0
    if delta <= 0.0:
        return 0, 0
    nx, ny = _lateral_grid(project)
    # The native generator walks the active level-set band, whose lower edge is
    # roughly one cell below the lowest surface point, plus the requested cover
    # region. Four extra rows and two extra lateral columns bound the extent
    # without knowing the final surface.
    rows = math.ceil((height + _structure_vertical_extent(project)) / delta) + 4
    lateral = math.ceil(nx) + 2
    if int(project.get("dimension") or 2) == 3:
        lateral *= math.ceil(ny) + 2
    cells = lateral * max(rows, 2)
    fmt = str(cell_set.get("format") or "both")
    per_step = 0
    if fmt in {"vtu", "both"}:
        per_step += cells * CELL_SET_VTU_BYTES_PER_CELL + 8192
    if fmt in {"json", "both"}:
        per_step += CELL_SET_JSON_BYTES
    return per_step * max(1, step_count), cells


def _step_cell_set_estimate(project: dict[str, Any]) -> tuple[int, int]:
    """Return ``(bytes, cells_upper_bound)`` for cell-set field steps.

    ``implant``/``anneal``/``analysis`` write the native ``Domain`` cell set,
    which is a different artifact from ``outputSettings.cellSet``: one VTU plus
    one summary per step. The vertical extent comes from the same conservative
    structure span used for the generic cell set, because the native generator
    walks the stored level-set band up to ``height`` above the surface.
    """
    total = 0
    largest = 0
    domain = project.get("domain") or {}
    try:
        delta = float(domain.get("gridDelta") or 0.0)
    except (TypeError, ValueError):
        return 0, 0
    if delta <= 0.0:
        return 0, 0
    nx, ny = _lateral_grid(project)
    lateral = math.ceil(nx) + 2
    if int(project.get("dimension") or 2) == 3:
        lateral *= math.ceil(ny) + 2
    for step in _enabled_steps(project):
        if step.get("model") not in {"implant", "anneal", "analysis"}:
            continue
        settings = step.get("cellSet")
        if not isinstance(settings, dict):
            continue
        try:
            height = max(0.0, float(settings.get("height") or 0.0))
        except (TypeError, ValueError):
            continue
        try:
            substrate = max(0.0, float(settings.get("substrateDepth") or 0.0))
        except (TypeError, ValueError):
            substrate = 0.0
        rows = math.ceil((height + substrate + _structure_vertical_extent(project)) / delta) + 4
        cells = lateral * max(rows, 2)
        largest = max(largest, cells)
        fmt = str(settings.get("format") or "both")
        if fmt in {"vtu", "both"}:
            total += cells * CELL_SET_VTU_BYTES_PER_CELL + 8192
        if fmt in {"json", "both"}:
            total += CELL_SET_JSON_BYTES
    return total, largest


def _optional_outputs(project: dict[str, Any], *, cells: float, step_count: int,
                      all_snapshot_bytes: int) -> tuple[int, list[str], int, int]:
    settings = project.get("outputSettings") or {}
    dimension = int(project.get("dimension") or 2)
    levelsets = _levelset_count(project)
    total = 0.0
    contributors: list[str] = []

    if settings.get("surfaceMeshVtk"):
        # One VTK file per step. The legacy VTK payload is close to the JSON
        # snapshot in size (calibrated on the 5x5 um fin runs).
        total += all_snapshot_bytes
        contributors.append("surfaceMeshVtk")
    if settings.get("volumeMesh") and dimension == 3:
        total += VOLUME_MESH_BYTES_PER_CELL * cells * step_count
        contributors.append("volumeMesh")
    if settings.get("levelSets"):
        total += LEVEL_SET_BYTES_PER_CELL * cells * step_count * levelsets
        contributors.append("levelSets")
    if settings.get("levelSetMeshes"):
        total += LEVEL_SET_MESH_BYTES_PER_CELL * cells * step_count * levelsets
        contributors.append("levelSetMeshes")
    if settings.get("nativeMetadata"):
        total += NATIVE_METADATA_BYTES_PER_CELL * cells * step_count
        contributors.append("nativeMetadata")
    cell_set_bytes, cell_set_cells = _cell_set_estimate(project, step_count=step_count)
    if cell_set_bytes:
        total += cell_set_bytes
        contributors.append(f"cellSet（至多 {cell_set_cells:,} 个单元/步）")
    step_cell_set_bytes, step_cell_set_cells = _step_cell_set_estimate(project)
    if step_cell_set_bytes:
        total += step_cell_set_bytes
        contributors.append(f"implant/anneal/analysis cellSet（至多 {step_cell_set_cells:,} 个单元/步）")
        cell_set_cells = max(cell_set_cells, step_cell_set_cells)
    return int(round(total)), contributors, cell_set_bytes, cell_set_cells


def _confidence(project: dict[str, Any]) -> str:
    settings = project.get("outputSettings") or {}
    # No calibration data for full-grid level-set exports.
    if settings.get("levelSets") or settings.get("levelSetMeshes"):
        return "low"
    geometry = _geometry_kind(project)
    if int(project.get("dimension") or 2) != 3 or geometry not in {"gds", "fin"}:
        return "medium"
    if settings.get("volumeMesh") or settings.get("surfaceMeshVtk") or settings.get("nativeMetadata"):
        return "medium"
    return "high"


def _suggestions(project: dict[str, Any], estimate: dict[str, Any]) -> list[str]:
    suggestions: list[str] = []
    cap = estimate["capBytes"]
    util = estimate["utilization"]
    dimension = int(project.get("dimension") or 2)
    domain = project.get("domain") or {}
    try:
        delta = float(domain.get("gridDelta") or 0.0)
    except (TypeError, ValueError):
        delta = 0.0
    enabled = _enabled_steps(project)
    settings = project.get("outputSettings") or {}

    if delta > 0 and util > WARNING_FRACTION:
        # Footprint scales as 1/delta^2 in 3D and 1/delta in 2D. Target 90 %
        # of the cap so a warning does not immediately return.
        factor = math.sqrt(util / 0.9) if dimension == 3 else util / 0.9
        if factor > 1.01:
            suggestions.append(
                f"将 gridDelta 从 {delta:g} 提高到至少 {delta * factor:.4g} um"
            )
    if len(enabled) > 1 and util > WARNING_FRACTION:
        # State and snapshot totals grow roughly quadratically with step count.
        keep = int(len(enabled) * math.sqrt(0.9 / max(util, 1e-9)))
        disable = max(1, len(enabled) - max(1, keep))
        suggestions.append(
            f"禁用或合并至少 {disable} 个（共 {len(enabled)} 个）已启用步骤"
        )
    optional = sorted(
        key for key, value in settings.items()
        if (value if isinstance(value, bool) else isinstance(value, dict) and bool(value.get("enabled")))
    )
    if optional:
        suggestions.append("禁用可选输出：" + "、".join(optional))
    cell_set = settings.get("cellSet")
    if isinstance(cell_set, dict) and cell_set.get("enabled") and (
            estimate.get("cellSetCellsUpperBound", 0) > MAX_CELL_SET_CELLS
            or estimate["risk"] == "reject" and estimate["estimatedBytes"] > cap):
        suggestions.append("降低 outputSettings.cellSet.height 或提高 gridDelta")
    if dimension == 3 and util > WARNING_FRACTION:
        suggestions.append("缩小 xExtent/yExtent（当前 "
                           f"{domain.get('xExtent')} x {domain.get('yExtent')} um）")
    if estimate["archiveEstimateBytes"] > cap and estimate["estimatedBytes"] <= cap:
        suggestions.append("避免一次下载全部步骤快照；合并后的 ZIP 已超过上限")
    if not suggestions:
        suggestions.append("无需调整；估算值低于警告阈值")
    return suggestions


def estimate_artifacts(project: dict[str, Any], *, kind: str = "simulate",
                       lazy_snapshots: bool | None = None) -> dict[str, Any]:
    """Return a structured artifact estimate for a normalized project.

    ``kind`` follows the run API: ``preview`` and ``slice`` execute no process
    steps, so only the initial state/snapshot are counted. ``simulate`` counts
    every enabled step. Unknown kinds are treated conservatively as simulate.
    """
    if kind in {"preview", "slice"}:
        process_steps = 0
    else:
        process_steps = len(_enabled_steps(project))
    if lazy_snapshots is None:
        lazy_snapshots = lazy_snapshots_enabled()

    dimension = int(project.get("dimension") or 2)
    geometry = _geometry_kind(project)
    cells = _lateral_cells(project)
    dimension_factor = 0.4 if dimension == 2 else 1.0
    state_factor = STATE_GEOMETRY_FACTOR.get(geometry, 0.5) * dimension_factor
    snapshot_factor = SNAPSHOT_GEOMETRY_FACTOR.get(geometry, 1.2) * dimension_factor
    layer_factor = _layer_growth(project)
    step_count = process_steps + 1  # initial state/snapshot + enabled steps
    growth_units = sum(_step_weight(step) for step in _enabled_steps(project))

    # Native states: one per executed step, growing with the step index.
    state_bytes = int(round(
        cells * state_factor * layer_factor
        * step_count * (STATE_BASE_BYTES_PER_CELL
                        + STATE_GROWTH_BYTES_PER_CELL * (step_count - 1) / 2.0)
    ))
    initial_snapshot_bytes = int(round(
        cells * snapshot_factor * layer_factor * INITIAL_SNAPSHOT_BYTES_PER_CELL
    ))
    largest_step_snapshot_bytes = 0
    if process_steps:
        largest_step_snapshot_bytes = int(round(
            cells * snapshot_factor * layer_factor
            * (STEP_SNAPSHOT_BYTES_PER_CELL + STEP_SNAPSHOT_GROWTH_BYTES_PER_CELL * growth_units)
        ))
    all_snapshots_bytes = _all_snapshot_bytes(cells, snapshot_factor, layer_factor, step_count)
    optional_bytes, optional_kinds, cell_set_bytes, cell_set_cells = _optional_outputs(
        project, cells=cells, step_count=step_count, all_snapshot_bytes=all_snapshots_bytes
    )

    if lazy_snapshots:
        snapshot_bytes = initial_snapshot_bytes + largest_step_snapshot_bytes
    else:
        # Every step snapshot is written eagerly, matching a worker started
        # with VIENNAPS_WORKBENCH_LAZY_SNAPSHOTS=0.
        snapshot_bytes = all_snapshots_bytes

    raw_primary = state_bytes + snapshot_bytes + optional_bytes
    raw_archive = state_bytes + all_snapshots_bytes + optional_bytes
    estimated_bytes = int(round(raw_primary * SAFETY_FACTOR))
    archive_bytes = int(round(raw_archive * SAFETY_FACTOR))
    confidence = _confidence(project)
    cap_bytes = artifact_limit_bytes()
    utilization = estimated_bytes / cap_bytes

    risk = "ok"
    downgraded = False
    cell_set_forced_reject = bool(
        cell_set_cells and (utilization > REJECT_FRACTION or cell_set_cells > MAX_CELL_SET_CELLS)
    )
    if cell_set_forced_reject:
        # A requested cell set is a real, measurable artifact. Never let an
        # unrelated low-confidence output downgrade an over-cap run (or a cell
        # set above the hard construction limit) to a warning: reject before
        # the worker starts writing.
        risk = "reject"
        downgraded = False
    elif utilization > REJECT_FRACTION:
        if confidence == "low":
            # No calibration for this output mode: warn instead of blocking.
            risk = "warning"
            downgraded = True
        else:
            risk = "reject"
    elif utilization >= WARNING_FRACTION:
        risk = "warning"
    # The archive is informational: the run cap applies to the run directory,
    # not to the ZIP. A ZIP of every materialized snapshot may still be refused
    # with 413 even for an allowed run, so surface it separately instead of
    # turning an otherwise normal run into a warning.
    archive_risk = "warning" if archive_bytes > cap_bytes else "ok"

    contributors = [
        {"kind": "nativeStates", "bytes": int(round(state_bytes * SAFETY_FACTOR)),
         "detail": f"{step_count} 个原生 .vpsd 状态，横向网格 {cells:,.0f} 个"},
        {"kind": "initialSnapshot", "bytes": int(round(initial_snapshot_bytes * SAFETY_FACTOR)),
         "detail": "立即写入的初始表面快照"},
        {"kind": "stepSnapshots", "bytes": int(round((snapshot_bytes - initial_snapshot_bytes) * SAFETY_FACTOR)),
         "detail": ("首次读取时物化的最大延迟快照" if lazy_snapshots
                    else "全部立即写入的步骤快照")},
    ]
    if optional_bytes:
        contributors.append({
            "kind": "optionalOutputs", "bytes": int(round(optional_bytes * SAFETY_FACTOR)),
            "detail": "、".join(optional_kinds),
        })
    if archive_risk == "warning":
        contributors.append({
            "kind": "archiveAllSnapshots", "bytes": archive_bytes,
            "detail": "下载 ZIP 会物化所有延迟快照",
        })

    estimate = {
        "schemaVersion": 1,
        "estimatedBytes": estimated_bytes,
        "estimatedMiB": round(estimated_bytes / MIB, 1),
        "archiveEstimateBytes": archive_bytes,
        "archiveEstimateMiB": round(archive_bytes / MIB, 1),
        "capBytes": cap_bytes,
        "capMiB": cap_bytes // MIB,
        "utilization": round(utilization, 3),
        "risk": risk,
        "archiveRisk": archive_risk,
        "confidence": confidence,
        "rejectionDowngraded": downgraded,
        "lazySnapshots": lazy_snapshots,
        "kind": kind,
        "safetyFactor": SAFETY_FACTOR,
        "warningFraction": WARNING_FRACTION,
        "rejectFraction": REJECT_FRACTION,
        "contributors": contributors,
        "suggestions": [],
    }
    if cell_set_cells:
        estimate["cellSetCellsUpperBound"] = cell_set_cells
    estimate["suggestions"] = _suggestions(project, estimate)
    return estimate


def should_reject(estimate: dict[str, Any]) -> bool:
    return estimate.get("risk") == "reject"


def warning_message(estimate: dict[str, Any]) -> str | None:
    if estimate.get("risk") == "ok":
        return None
    utilization = estimate.get("utilization", 0.0)
    percent = int(round(utilization * 100))
    if estimate.get("risk") == "reject":
        prefix = (f"运行产物预检估算本次运行约 {estimate['estimatedMiB']:.0f} MiB"
                  f"（占 {estimate['capMiB']} MiB 上限的 {percent}%），已超过上限。"
                  "POST /api/runs 会在工作进程启动前拒绝该项目。")
    elif estimate.get("rejectionDowngraded"):
        prefix = (f"运行产物预检估算本次运行约 {estimate['estimatedMiB']:.0f} MiB"
                  f"（占 {estimate['capMiB']} MiB 上限的 {percent}%），已超过上限。"
                  "该配置使用未校准的输出模式，因此仅给出警告，不会拒绝运行。")
    else:
        prefix = (f"运行产物预检估算本次运行约 {estimate['estimatedMiB']:.0f} MiB"
                  f"（占 {estimate['capMiB']} MiB 上限的 {percent}%）。")
    details = []
    if estimate.get("archiveEstimateBytes", 0) > estimate.get("capBytes", 0):
        details.append(
            f"若物化并下载全部步骤快照，预计约 "
            f"{estimate['archiveEstimateMiB']:.0f} MiB，下载可能返回 HTTP 413。"
        )
    suggestions = estimate.get("suggestions") or []
    if suggestions:
        details.append("建议：" + "；".join(suggestions) + "。")
    return "".join([prefix, *details])


def rejection_detail(estimate: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": "artifact-budget-preflight",
        "message": warning_message(estimate),
        "estimatedBytes": estimate["estimatedBytes"],
        "capBytes": estimate["capBytes"],
        "suggestions": estimate["suggestions"],
        "artifactEstimate": estimate,
    }
