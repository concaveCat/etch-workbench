"""Versioned tolerance comparison for two 2D result snapshots."""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import Field

from ..models import StrictModel
from .metrology import snapshot_content_hash


Hash64 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Id = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class ComparisonProtocolV1(StrictModel):
    schemaVersion: Literal[1]
    protocolId: Id
    version: Annotated[int, Field(ge=1)]
    pointTolerance: Annotated[float, Field(gt=0)]
    metricTolerances: dict[str, Annotated[float, Field(ge=0)]] = Field(default_factory=dict)
    methodVersion: Annotated[str, Field(min_length=1, max_length=32)] = "snapshot-point-v1"
    checkType: Literal["snapshot", "same_segment_replay", "continuous_vs_segmented", "prefix_reuse"] = "snapshot"


class EquivalenceResultV1(StrictModel):
    schemaVersion: Literal[1]
    checkId: Id | None = None
    checkType: Literal["snapshot", "same_segment_replay", "continuous_vs_segmented", "prefix_reuse"] = "snapshot"
    protocolId: Id
    protocolVersion: Annotated[int, Field(ge=1)]
    leftHash: Hash64
    rightHash: Hash64
    leftDigest: Hash64 | None = None
    rightDigest: Hash64 | None = None
    leftRunId: str | None = None
    rightRunId: str | None = None
    leftSegmentId: str | None = None
    rightSegmentId: str | None = None
    leftStepId: str | None = None
    rightStepId: str | None = None
    statisticalStatus: Literal["not_evaluated"] = "not_evaluated"
    status: Literal["established", "failed", "inconclusive"]
    maxPointDelta: float | None = None
    materialMismatch: bool = False
    reasons: list[str] = Field(default_factory=list)


def compare_snapshots(protocol: ComparisonProtocolV1, left: dict[str, Any], right: dict[str, Any]) -> EquivalenceResultV1:
    left_hash = snapshot_content_hash(left)
    right_hash = snapshot_content_hash(right)
    reasons: list[str] = []
    if left.get("dimension") != 2 or right.get("dimension") != 2:
        reasons.append("P0 equivalence supports 2D snapshots only")
    if left.get("topology") != right.get("topology"):
        reasons.append("topology differs")
    if left.get("axisMap") != right.get("axisMap"):
        reasons.append("axisMap differs")
    if left.get("coordinateUnit") != right.get("coordinateUnit"):
        reasons.append("coordinateUnit differs")
    if left.get("gridDelta") != right.get("gridDelta"):
        reasons.append("gridDelta differs")
    if left.get("lines") != right.get("lines"):
        reasons.append("line connectivity differs")
    if left.get("materials") != right.get("materials"):
        reasons.append("material catalog differs")
    left_points = left.get("points") or []
    right_points = right.get("points") or []
    if len(left_points) != len(right_points):
        reasons.append("point counts differ")
        return EquivalenceResultV1(
            schemaVersion=1,
            checkType=protocol.checkType,
            protocolId=protocol.protocolId,
            protocolVersion=protocol.version,
            leftHash=left_hash,
            rightHash=right_hash,
            leftDigest=left_hash,
            rightDigest=right_hash,
            status="failed",
            materialMismatch=False,
            reasons=reasons,
        )
    left_ids = (left.get("cellData") or {}).get("materialIds") or []
    right_ids = (right.get("cellData") or {}).get("materialIds") or []
    material_mismatch = left_ids != right_ids
    if material_mismatch:
        reasons.append("line material assignment differs")
    max_delta = 0.0
    axis_map = left.get("axisMap") or {"x": 0, "h": 1, "z": 2}
    try:
        xi, hi = int(axis_map["x"]), int(axis_map["h"])
        for first, second in zip(left_points, right_points):
            dx = float(first[xi]) - float(second[xi])
            dh = float(first[hi]) - float(second[hi])
            max_delta = max(max_delta, math.hypot(dx, dh))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        reasons.append(f"snapshot coordinates are invalid: {exc}")
        return EquivalenceResultV1(
            schemaVersion=1,
            checkType=protocol.checkType,
            protocolId=protocol.protocolId,
            protocolVersion=protocol.version,
            leftHash=left_hash,
            rightHash=right_hash,
            leftDigest=left_hash,
            rightDigest=right_hash,
            status="inconclusive",
            maxPointDelta=None,
            materialMismatch=material_mismatch,
            reasons=reasons,
        )
    if max_delta > protocol.pointTolerance:
        reasons.append(f"maximum point delta {max_delta:.6g} exceeds {protocol.pointTolerance:.6g}")
    status = "established" if not reasons else "failed"
    return EquivalenceResultV1(
        schemaVersion=1,
        checkType=protocol.checkType,
        protocolId=protocol.protocolId,
        protocolVersion=protocol.version,
        leftHash=left_hash,
        rightHash=right_hash,
        leftDigest=left_hash,
        rightDigest=right_hash,
        status=status,
        maxPointDelta=max_delta,
        materialMismatch=material_mismatch,
        reasons=reasons,
    )
