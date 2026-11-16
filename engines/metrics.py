"""Geometry descriptors used by the comparison record.

Phase A deliberately reports **per-engine self descriptors** instead of a
cross-engine distance: SPX currently returns a rendered frame, not a structure, so
any mesh-vs-raster distance would be fabricated. A cross-engine probe lattice
(common Δx, material occupancy, interface distance) needs a structure export from
SPX and lands in phase M1.

``not_evaluated`` is a first-class result: a metric that cannot be computed is
reported as such, never as zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

NOT_EVALUATED = "not_evaluated"


def mesh_descriptors(path: Path) -> dict[str, Any]:
    """Per-material top height and surface triangle count for a mesh frame."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "unreadable", "detail": str(exc)}
    points = payload.get("points") or []
    triangles = payload.get("triangles") or []
    ids = payload.get("materialIds") or []
    names = {int(row["id"]): row["name"] for row in payload.get("materials") or []}
    if not points or not triangles or len(ids) != len(triangles):
        return {"status": "empty"}
    top: dict[str, float] = {}
    for triangle, material_id in zip(triangles, ids):
        name = names.get(int(material_id), str(material_id))
        z = max(float(points[index][2]) for index in triangle)
        if name not in top or z > top[name]:
            top[name] = z
    return {
        "status": "measured",
        "triangleCount": len(triangles),
        "pointCount": len(points),
        # Heights are relative to the substrate top (z = 0) in um.
        "topHeightUm": {name: round(value, 6) for name, value in sorted(top.items())},
    }


def raster_descriptors(path: Path) -> dict[str, Any]:
    """Frame-only descriptors. Material geometry is not recoverable from a PNG."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"status": "unreadable", "detail": str(exc)}
    return {
        "status": "frame_only",
        "bytes": size,
        "geometry": NOT_EVALUATED,
        "reason": "the SPX adapter returns a rendered frame; structure export is phase M1",
    }


def describe(frame_path: Path) -> dict[str, Any]:
    if frame_path.suffix == ".json":
        return mesh_descriptors(frame_path)
    return raster_descriptors(frame_path)
