"""Engine-neutral snapshot writer for v2 engine plugins.

The rich ViennaPS snapshot writer remains in ``worker.simulate`` because it
also reports native measurements, material tops, and optional VTK artifacts.
An engine that supplies a ``SurfaceBackend`` uses this smaller projection:
the viewer still receives points, cells, material IDs, bounds and step
metadata, while the engine's per-step state/replay artifacts are written by
``FlowExecutor``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from process_models.v2.surface import SurfaceBackend, SurfaceData
from worker.artifacts import atomic_json, check_artifact_budget


def save_surface_snapshot(
    ctx: Any,
    state: Any,
    backend: SurfaceBackend,
    *,
    step: dict[str, Any],
    step_index: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one generic surface snapshot and return its metadata."""
    surface = backend.extract(state)
    if not isinstance(surface, SurfaceData):
        raise TypeError(f"surface backend {getattr(backend, 'id', backend)!r} did not return SurfaceData")

    is_3d = surface.dimension == 3
    points = [list(point) for point in surface.points]
    cells = [list(cell) for cell in surface.cells]
    material_ids = [int(value) for value in surface.material_ids]
    bounds = surface.bounds or _bounds(surface.points)
    if bounds is None:
        surface_bounds = None
    elif is_3d:
        surface_bounds = list(bounds)
    else:
        surface_bounds = [[bounds[0], bounds[1], 0.0], [bounds[3], bounds[4], 0.0]]

    names = dict(state.materials.names)
    materials = {}
    for material_id in sorted(set(material_ids)):
        name = surface.materials.get(material_id) if surface.materials else None
        materials[str(material_id)] = {
            "name": name if name is not None else names.get(material_id, f"Material {material_id}"),
            "levelSetIndexes": [],
            "sourceStepId": None,
        }

    cells_key = "triangles" if is_3d else "lines"
    count_key = "triangleCount" if is_3d else "lineCount"
    snapshot_name = f"{step['id']}.json"
    payload = {
        "schemaVersion": int(ctx.project.get("schemaVersion", 7)),
        "runId": ctx.run_id,
        "stepId": step["id"],
        "stepName": step["name"],
        "stepIndex": step_index,
        "axisMap": {"x": 0, "y": 1, "z": 2} if is_3d else {"x": 0, "h": 1, "z": 2},
        "coordinateUnit": str((ctx.project.get("units") or {}).get("length") or "um"),
        "points": points,
        cells_key: cells,
        "cellData": {"materialIds": material_ids},
        "materials": materials,
        "mesh": {
            "pointCount": len(points),
            count_key: len(cells),
            "topology": "triangles" if is_3d else "lines",
        },
        "gridDelta": float(surface.grid_delta),
        "materialIdsByLevelSetIndex": sorted(set(material_ids)),
        "sameMaterialInternalInterfacesHidden": False,
        "outputs": [],
        "noOp": False,
        "measuredGeometry": None,
        "materialTops": {},
        "dimension": 3 if is_3d else 2,
        "topology": "triangles" if is_3d else "lines",
        "surfaceBounds": surface_bounds,
        "surfaceBackend": str(getattr(backend, "id", "engine-surface")),
    }
    if extra:
        payload.update(extra)
    atomic_json(Path(ctx.run_dir) / snapshot_name, payload)
    check_artifact_budget(Path(ctx.run_dir))
    metadata = {
        "stepId": step["id"],
        "name": step["name"],
        "stepIndex": step_index,
        "snapshot": snapshot_name,
        "outputs": [],
        "pointCount": len(points),
        count_key: len(cells),
        "materialIds": sorted(set(material_ids)),
        "materialIdsByLevelSetIndex": sorted(set(material_ids)),
        "noOp": False,
        "measuredGeometry": None,
        "surfaceBounds": surface_bounds,
        "materialTops": {},
        "nativeState": None,
        **(extra or {}),
    }
    if is_3d:
        metadata["dimension"] = 3
        metadata["topology"] = "triangles"
    return metadata


def _bounds(points: tuple[tuple[float, float, float], ...]) -> tuple[float, float, float, float, float, float] | None:
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    zs = [point[2] for point in points]
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
