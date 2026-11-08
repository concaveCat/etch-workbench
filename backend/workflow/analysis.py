"""P3 snapshot comparison, process windows and provenance reports."""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from .. import snapshots, storage
from .metrology import snapshot_content_hash
from .store import WorkflowValidation

MAX_VIEW_POINTS = 20_000
MAX_VIEW_LINES = 30_000
MAX_VIEW_TRIANGLES = 10_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _report_time() -> str:
    """Human-facing report timestamp in the local timezone.

    The report used to print an ISO UTC string (`2026-10-31T05:40:00+00:00`).
    A reader expects wall-clock time with a timezone suffix, for example
    `2026-10-31 13:40 CST`.
    """

    local = datetime.now(timezone.utc).astimezone()
    return local.strftime("%Y-%m-%d %H:%M %Z").strip()


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def load_snapshot(run_id: str, step_id: str) -> dict[str, Any]:
    run_dir = storage.run_path(run_id)
    manifest = storage.read_json(run_dir / "manifest.json")
    if not any(item.get("stepId") == step_id for item in manifest.get("snapshots") or []):
        raise FileNotFoundError(f"snapshot {step_id!r} is not listed in run {run_id!r}")
    path = snapshots.ensure_materialized(run_dir, step_id)
    payload = storage.read_json(path)
    payload["runId"] = run_id
    payload["stepId"] = step_id
    return payload


def _material_names(snapshot: dict[str, Any]) -> dict[int, str]:
    return {int(key): str(value.get("name") or key) for key, value in (snapshot.get("materials") or {}).items()}


def _line_length(points: list[Any], line: list[int]) -> float:
    try:
        a, b = points[int(line[0])], points[int(line[1])]
        return math.dist([float(a[0]), float(a[1]), float(a[2]) if len(a) > 2 else 0.0],
                         [float(b[0]), float(b[1]), float(b[2]) if len(b) > 2 else 0.0])
    except (IndexError, TypeError, ValueError):
        return 0.0


def _triangle_area(points: list[Any], triangle: list[int]) -> float:
    try:
        a, b, c = (points[int(index)] for index in triangle)
        ax, ay, az = (float(value) for value in a[:3])
        bx, by, bz = (float(value) for value in b[:3])
        cx, cy, cz = (float(value) for value in c[:3])
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        cross = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
        return 0.5 * math.sqrt(sum(value * value for value in cross))
    except (IndexError, TypeError, ValueError):
        return 0.0


def snapshot_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    dimension = int(snapshot.get("dimension") or 0)
    points = snapshot.get("points") or []
    lines = snapshot.get("lines") or []
    triangles = snapshot.get("triangles") or []
    material_ids = (snapshot.get("cellData") or {}).get("materialIds") or []
    names = _material_names(snapshot)
    measures: dict[str, float] = {}
    if dimension == 2:
        for index, line in enumerate(lines):
            material_id = int(material_ids[index]) if index < len(material_ids) else -1
            name = names.get(material_id, str(material_id))
            measures[name] = measures.get(name, 0.0) + _line_length(points, line)
        measure_kind = "length_um"
    elif dimension == 3:
        for index, triangle in enumerate(triangles):
            material_id = int(material_ids[index]) if index < len(material_ids) else -1
            name = names.get(material_id, str(material_id))
            measures[name] = measures.get(name, 0.0) + _triangle_area(points, triangle)
        measure_kind = "area_um2"
    else:
        measure_kind = "unknown"
    xs = [float(point[0]) for point in points if len(point) >= 2]
    ys = [float(point[1]) for point in points if len(point) >= 2]
    zs = [float(point[2]) for point in points if len(point) >= 3]
    bounds = [
        min(xs, default=0.0), max(xs, default=0.0),
        min(ys, default=0.0), max(ys, default=0.0),
        min(zs, default=0.0), max(zs, default=0.0),
    ]
    return {
        "runId": snapshot.get("runId"),
        "stepId": snapshot.get("stepId"),
        "dimension": dimension,
        "topology": snapshot.get("topology"),
        "digest": snapshot_content_hash(snapshot),
        "pointCount": len(points),
        "lineCount": len(lines),
        "triangleCount": len(triangles),
        "bounds": bounds,
        "materialMeasures": {key: round(value, 9) for key, value in sorted(measures.items())},
        "measureKind": measure_kind,
        "connectivityHash": hashlib.sha256(json.dumps({"lines": lines, "triangles": triangles}, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "surfaceBounds": snapshot.get("surfaceBounds"),
        "materialTops": snapshot.get("materialTops"),
        "measuredGeometry": snapshot.get("measuredGeometry"),
        "measuredRemoval": snapshot.get("measuredRemoval"),
        "measuredAdded": snapshot.get("measuredAdded"),
    }


def _max_point_delta(left: dict[str, Any], right: dict[str, Any]) -> float | None:
    left_points, right_points = left.get("points") or [], right.get("points") or []
    if not left_points or len(left_points) != len(right_points):
        return None
    maximum = 0.0
    for a, b in zip(left_points, right_points):
        try:
            maximum = max(maximum, math.dist([float(value) for value in a[:3]], [float(value) for value in b[:3]]))
        except (TypeError, ValueError):
            return None
    return maximum


def _bounds_delta(left: list[Any], right: list[Any]) -> float:
    return max((abs(float(a) - float(b)) for a, b in zip(left, right)), default=0.0)


def compare_snapshots(
    left: dict[str, Any],
    right: dict[str, Any],
    tolerance: float,
    material_tolerance: float | None = None,
) -> dict[str, Any]:
    left_summary = snapshot_summary(left)
    right_summary = snapshot_summary(right)
    is_3d = left_summary["dimension"] == 3 or right_summary["dimension"] == 3
    effective_material_tolerance = material_tolerance if material_tolerance is not None else (tolerance * tolerance if is_3d else tolerance)
    reasons: list[str] = []
    if left_summary["digest"] == right_summary["digest"] and left_summary["connectivityHash"] == right_summary["connectivityHash"]:
        status = "identical"
    else:
        topology_equal = all(
            left_summary[key] == right_summary[key]
            for key in ("dimension", "topology", "pointCount", "lineCount", "triangleCount")
        )
        connectivity_equal = left_summary["connectivityHash"] == right_summary["connectivityHash"]
        if not topology_equal:
            reasons.append("snapshot topology or element counts differ")
        if not connectivity_equal:
            reasons.append("snapshot line/triangle connectivity differs")
        max_delta = _max_point_delta(left, right)
        bounds_delta = _bounds_delta(left_summary["bounds"], right_summary["bounds"])
        if max_delta is not None and max_delta > tolerance:
            reasons.append(f"max point delta {max_delta:.6g} exceeds tolerance {tolerance:.6g}")
        if bounds_delta > tolerance:
            reasons.append(f"bounds delta {bounds_delta:.6g} exceeds tolerance {tolerance:.6g}")
        material_delta = {
            key: round(right_summary["materialMeasures"].get(key, 0.0) - left_summary["materialMeasures"].get(key, 0.0), 9)
            for key in sorted(set(left_summary["materialMeasures"]) | set(right_summary["materialMeasures"]))
        }
        for key, delta in material_delta.items():
            if abs(delta) > effective_material_tolerance:
                reasons.append(f"material {key} measure delta {delta:.6g} exceeds material tolerance {effective_material_tolerance:.6g}")
        status = "within_tolerance" if topology_equal and connectivity_equal and not reasons else "different"
    material_delta = {
        key: round(right_summary["materialMeasures"].get(key, 0.0) - left_summary["materialMeasures"].get(key, 0.0), 9)
        for key in sorted(set(left_summary["materialMeasures"]) | set(right_summary["materialMeasures"]))
    }
    return {
        "status": status,
        "leftDigest": left_summary["digest"],
        "rightDigest": right_summary["digest"],
        "maxPointDelta": _max_point_delta(left, right),
        "boundsDelta": _bounds_delta(left_summary["bounds"], right_summary["bounds"]),
        "materialDelta": material_delta,
        "reasons": reasons,
        "left": left_summary,
        "right": right_summary,
        "tolerance": tolerance,
        "materialTolerance": effective_material_tolerance,
    }


def candidate_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    dimension = int(snapshot.get("dimension") or 0)
    points = snapshot.get("points") or []
    material_ids = (snapshot.get("cellData") or {}).get("materialIds") or []
    names = _material_names(snapshot)
    stride = max(1, math.ceil(len(points) / MAX_VIEW_POINTS)) if points and dimension == 3 else 1
    sampled_points = [[round(float(value), 9) for value in point[:3]] for point in points[::stride][:MAX_VIEW_POINTS]]
    sampled_materials: list[int] = []
    if dimension == 3 and snapshot.get("triangles"):
        vertex_materials: list[int] = []
        for index, triangle in enumerate(snapshot["triangles"]):
            material = int(material_ids[index]) if index < len(material_ids) else -1
            for vertex in triangle:
                while len(vertex_materials) <= int(vertex):
                    vertex_materials.append(-1)
                vertex_materials[int(vertex)] = material
        sampled_materials = [vertex_materials[index] if index < len(vertex_materials) else -1 for index in range(0, len(points), stride)]
    elif dimension == 2:
        sampled_materials = [int(value) for value in material_ids[:len(sampled_points)]]
    lines = snapshot.get("lines") or []
    triangles = snapshot.get("triangles") or []
    return {
        "dimension": dimension,
        "topology": snapshot.get("topology"),
        "points": sampled_points,
        "lines": lines[:MAX_VIEW_LINES] if dimension == 2 else [],
        "triangles": triangles[:MAX_VIEW_TRIANGLES] if dimension == 3 else [],
        "materialIds": sampled_materials,
        "lineMaterialIds": [int(value) for value in material_ids[:MAX_VIEW_LINES]] if dimension == 2 else [],
        "triangleMaterialIds": [int(value) for value in material_ids[:MAX_VIEW_TRIANGLES]] if dimension == 3 else [],
        "materials": {str(key): value for key, value in (snapshot.get("materials") or {}).items()},
        "materialNames": {str(key): value for key, value in names.items()},
        "pointCount": len(points),
        "lineCount": len(lines),
        "triangleCount": len(triangles),
        "sampledPointCount": len(sampled_points),
        "stride": stride,
        "axisMap": snapshot.get("axisMap") or {"x": 0, "h": 1, "z": 2},
        "coordinateUnit": snapshot.get("coordinateUnit") or "um",
        "bounds": snapshot_summary(snapshot)["bounds"],
    }


def process_window(document: dict[str, Any], lower: float, upper: float) -> dict[str, Any]:
    rows = list((document.get("aggregation") or {}).get("rows") or [])
    classified = []
    for row in rows:
        mean, ci = _number(row.get("mean")), _number(row.get("ci95"))
        margin = None if mean is None else min(mean - lower, upper - mean)
        n = row.get("n")
        if mean is None or n is None or int(n) < 2 or ci is None:
            status = "not_evaluated"
        elif ci is not None and (mean + ci < lower or mean - ci > upper):
            status = "fail"
        elif ci is not None and (mean - ci < lower or mean + ci > upper):
            status = "uncertain"
        elif ci is None and (mean < lower or mean > upper):
            status = "fail"
        else:
            status = "pass"
        classified.append({
            "ordinal": row.get("ordinal"), "variantId": row.get("variantId"),
            "parameters": row.get("parameters") or {}, "mean": mean, "ci95": ci,
            "n": row.get("n"), "statisticalStatus": row.get("statisticalStatus"), "status": status,
            "margin": margin, "unit": row.get("unit"),
        })
    passing = [row for row in classified if row["status"] == "pass"]
    passing.sort(key=lambda row: row["margin"] if row["margin"] is not None else -math.inf, reverse=True)
    factor_windows = []
    factors = (document.get("design") or {}).get("factors") or []
    if not factors:
        discovered: set[tuple[str, str]] = set()
        for variant in document.get("variants") or []:
            for step_id, values in (variant.get("parameters") or {}).items():
                if isinstance(values, dict):
                    for parameter in values:
                        discovered.add((str(step_id), str(parameter)))
        factors = [{"stepId": step_id, "parameter": parameter} for step_id, parameter in sorted(discovered)]
    for factor in factors:
        values = []
        for row in passing:
            value = ((row.get("parameters") or {}).get(factor["stepId"]) or {}).get(factor["parameter"])
            if _number(value) is not None:
                values.append(float(value))
        factor_windows.append({
            "stepId": factor["stepId"], "parameter": factor["parameter"],
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        })
    return {
        "experimentId": document.get("experimentId"),
        "metricId": document.get("metricId"),
        "unit": document.get("unit"),
        "lower": lower, "upper": upper,
        "summary": {
            "total": len(classified),
            "pass": len(passing),
            "fail": sum(1 for row in classified if row["status"] == "fail"),
            "uncertain": sum(1 for row in classified if row["status"] == "uncertain"),
            "notEvaluated": sum(1 for row in classified if row["status"] == "not_evaluated"),
        },
        "factorWindows": factor_windows,
        "candidates": passing,
        "rows": classified,
        "generatedAt": _now(),
    }


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


# Status codes are part of the data contract; the report shows Chinese labels and
# keeps the raw code available in the technical-details blocks.
STATUS_LABELS = {
    "succeeded": "成功",
    "failed": "失败",
    "running": "运行中",
    "planned": "待执行",
    "queued": "排队中",
    "cancelled": "已取消",
    "interrupted": "已中断",
    "completed": "已完成",
    "WITHIN_TOLERANCE": "在公差内",
    "OUTSIDE_TOLERANCE": "超出公差",
    "NOT_EVALUATED": "未评估",
    "within_tolerance": "在公差内",
    "outside_tolerance": "超出公差",
    "not_evaluated": "未评估",
    "identical": "一致",
    "different": "不同",
    "measured": "已测量",
    "ambiguous": "测量有歧义",
    "no_intersection": "无交点",
    "region_empty": "区域为空",
    "material_not_found": "材料缺失",
    "unsupported_dimension": "维度不支持",
    "unsupported_units": "单位不支持",
    "invalid_geometry": "几何无效",
    "evaluated": "已评估",
    "pass": "通过",
    "fail": "超出",
    "uncertain": "不确定",
    "unbound": "未绑定",
    "verified": "已验证",
    "established": "已建立",
    "inconclusive": "不确定",
}

_PARAMETER_UNITS = {
    "duration": " s",
    "rateMagnitude": " µm/s",
    "stickingProbability": "",
    "sourceExponent": "",
}

_MATERIAL_PALETTE = (
    "#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2",
    "#edc948", "#b07aa1", "#9c755f", "#bab0ac", "#ff9da7",
)


def _status_label(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "未记录"
    text = str(value)
    return STATUS_LABELS.get(text, text)


# One mapping for 03/05/report/CSV; the technical id stays in parentheses/details.
METRIC_METHOD_LABELS = {
    "etch_depth": "刻蚀深度",
    "cd_at_height": "指定高度 CD",
}


def _method_label(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "未记录"
    text = str(value)
    return METRIC_METHOD_LABELS.get(text, text)


def _display(value: Any, empty: str = "未记录") -> str:
    """Render a readable cell value, never a blank or ``None``."""

    if value is None:
        return empty
    if isinstance(value, str) and not value.strip():
        return empty
    if isinstance(value, (list, tuple, dict, set)) and not value:
        return empty
    return _escape(value)


def _unit_text(value: Any) -> str:
    """Report units use the micro sign; the wire format keeps plain ``um``."""

    text = _display(value, "")
    return "µm" if text in {"um", "µm"} else text


# Raw API tokens that used to leak into the report body. Status codes keep
# their own labels in ``STATUS_LABELS``; these are seed policies, engine
# markers and uncertainty values.
_TOKEN_LABELS = {
    "not-applicable": "不适用",
    "not_applicable": "不适用",
    "shared-run-seed": "共享运行种子",
    "ambiguous": "有歧义",
}


def _token_label(value: Any, empty: str = "未记录") -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return empty
    text = str(value)
    return _TOKEN_LABELS.get(text, STATUS_LABELS.get(text, _escape(text)))


def _fmt_number(value: Any) -> str:
    """Report numbers keep 4 significant digits (0.200006 reads as 0.2)."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return "未记录"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        number = float(value)
        if number.is_integer() and abs(number) < 1e15:
            return str(int(number))
        return f"{number:.4g}"
    return _escape(value)


def _sig4(value: Any) -> str:
    """Bare 4-significant-digit number for CSV cells; empty when there is no value."""

    number = _number(value)
    if number is None:
        return ""
    if number == 0:
        return "0"
    return f"{number:.4g}"


def _code(value: Any, empty: str = "未记录") -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return empty
    return f"<code>{_escape(value)}</code>"


# Metrology reasons are stored as English API codes. Reports must show Chinese
# prose; the raw code only survives in the technical-details block.
_MEASUREMENT_REASON_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^multiple selected-material surfaces cross the measurement x$"),
     "测量位置 X 处有多条材料界面相交，无法确定唯一深度；请移动 X 位置或更换测量材料。"),
    (re.compile(r"^no material surface found at the measurement x$"),
     "测量位置 X 处没有找到材料界面。"),
    (re.compile(r"^no selected-material segment crosses the measurement x$"),
     "测量位置 X 处没有所选材料的界面。"),
    (re.compile(r"^isotropic model does not use a ray flux engine$"),
     "各向同性模型不使用射线通量引擎。"),
    (re.compile(r"^a selected-material segment lies on the measurement height$"),
     "所选材料的界面正好落在测量高度上。"),
    (re.compile(r"^fewer than two selected-material crossings at the measurement height$"),
     "测量高度上少于两个所选材料交点。"),
    (re.compile(r"^etch-depth x coordinate is outside the measurement region$"),
     "深度测量的 X 坐标超出测量区域。"),
    (re.compile(r"^material '?([^']+)'? is not present in the snapshot$"),
     "快照中没有材料 {0}。"),
    (re.compile(r"^line and material-id counts differ$"),
     "快照的线与材料编号数量不一致。"),
    (re.compile(r"^snapshot requires points, lines and cellData\.materialIds$"),
     "快照缺少点、线或材料编号。"),
    (re.compile(r"^3D material top-height summary has no finite height$"),
     "3D 材料顶面高度不是有限值。"),
    (re.compile(r"^3D metrology v1 supports material top-height depth only$"),
     "3D 测量仅支持材料顶面高度深度。"),
    (re.compile(r"^P0-3 metrology supports 2D and 3D-depth snapshots only$"),
     "测量仅支持 2D 与 3D 深度快照。"),
    (re.compile(r"^P0-3 V1 requires snapshot coordinates in um; normalize before measuring$"),
     "快照坐标必须是 µm，请先归一化。"),
)
_MEASUREMENT_REASON_FALLBACK = "测量无有效值"

_ENGINE_REASON_LABELS = {
    "isotropic model does not use a ray flux engine": "各向同性模型不使用射线通量引擎。",
    "directional model does not use a stochastic ray flux engine": "定向模型不使用随机射线通量引擎。",
    "native model does not use a stochastic ray flux engine": "原生模型不使用随机射线通量引擎。",
    "pinned per-step verified engine; no fallback": "按步骤固定使用已验证引擎，不做回退。",
    "pinned concrete engine for ViennaPS Process.calculateFlux; no fallback": "固定使用已验证的具体引擎执行 calculateFlux，不做回退。",
    "native geometry operation; no process engine or physical duration": "原生几何操作：不使用工艺引擎，也没有物理时长。",
    "zero rate; ViennaPS Process was not called": "速率为 0：未调用 ViennaPS Process。",
}


def _engine_reason_text(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "未记录"
    text = str(value)
    return _ENGINE_REASON_LABELS.get(text, text)


def _reason_text(reason: Any) -> str:
    """Chinese explanation for a metrology reason code."""

    if reason is None or (isinstance(reason, str) and not reason.strip()):
        return _MEASUREMENT_REASON_FALLBACK
    text = str(reason)
    for pattern, template in _MEASUREMENT_REASON_PATTERNS:
        match = pattern.match(text)
        if match:
            return template.format(*match.groups()) if match.groups() else template
    return _MEASUREMENT_REASON_FALLBACK


def _reason_detail(reason: Any) -> str | None:
    """Raw English reason, only when it has no Chinese mapping."""

    if reason is None or (isinstance(reason, str) and not reason.strip()):
        return None
    text = str(reason)
    return None if any(pattern.match(text) for pattern, _ in _MEASUREMENT_REASON_PATTERNS) else text


def _measurement_has_value(measurement: Any) -> bool:
    """Only a `measured` result carries a number; everything else is ambiguous/failed."""

    return getattr(measurement, "status", None) == "measured" and _number(getattr(measurement, "value", None)) is not None


def _details(rows: list[tuple[str, str]], summary: str = "技术详情") -> str:
    if not rows:
        return ""
    body = "".join(f"<tr><th>{_escape(label)}</th><td>{value}</td></tr>" for label, value in rows)
    return f'<details class="tech"><summary>{_escape(summary)}</summary><table>{body}</table></details>'


def _kv_table(rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><th>{_escape(label)}</th><td>{value}</td></tr>" for label, value in rows)
    return f"<table>{body}</table>"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_escape(header)}</th>" for header in headers)
    body = "".join("<tr>%s</tr>" % "".join(f"<td>{cell}</td>" for cell in row) for row in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def _step_names(compile: Any, manifest: dict[str, Any] | None) -> dict[str, str]:
    names: dict[str, str] = {}
    project = getattr(compile, "project", None) if compile is not None else None
    for step in getattr(project, "steps", None) or []:
        if getattr(step, "id", None) and getattr(step, "name", None):
            names[str(step.id)] = str(step.name)
    if manifest:
        for snapshot in manifest.get("snapshots") or []:
            step_id, name = snapshot.get("stepId"), snapshot.get("name")
            if step_id and name:
                names.setdefault(str(step_id), str(name))
    return names


def _step_label(step_id: Any, names: dict[str, str]) -> str:
    if step_id is None or (isinstance(step_id, str) and not step_id.strip()):
        return "未记录"
    return names.get(str(step_id)) or str(step_id)


def _condition_values(parameters: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for overrides in (parameters or {}).values():
        if not isinstance(overrides, dict):
            continue
        for key, value in sorted(overrides.items()):
            unit = _PARAMETER_UNITS.get(str(key), "")
            values.append(f"{_fmt_number(value)}{unit}")
    return values


def _variant_conditions(variant: dict[str, Any]) -> str:
    return " · ".join(_condition_values(variant.get("parameters") or {}))


def _variant_seed(variant: dict[str, Any]) -> Any:
    replicates = variant.get("replicates") or []
    for replicate in replicates:
        if replicate.get("seed") is not None:
            return replicate["seed"]
    return None


def _variant_title(variant: dict[str, Any], index: int) -> str:
    """Readable label: the conditions, never an opaque "candidate 1"."""

    parts: list[str] = []
    conditions = _variant_conditions(variant)
    if conditions:
        parts.append(conditions)
    else:
        parts.append(f"V{_fmt_number(variant.get('ordinal') or index + 1)}")
    seed = _variant_seed(variant)
    if seed is not None:
        parts.append(f"seed {_fmt_number(seed)}")
    return " · ".join(parts)


def _variant_status(variant: dict[str, Any]) -> str:
    statuses = [str((replicate or {}).get("status") or "planned") for replicate in variant.get("replicates") or []]
    if not statuses:
        return "未记录"
    if any(status == "running" for status in statuses):
        return "运行中"
    if any(status == "failed" for status in statuses):
        return "失败"
    if all(status == "succeeded" for status in statuses):
        return "成功"
    if all(status in {"planned", "queued"} for status in statuses):
        return "待执行"
    return _status_label(statuses[-1])


def _material_name(view: dict[str, Any], material_id: Any) -> str:
    name = (view.get("materialNames") or {}).get(str(material_id))
    if name:
        return str(name)
    entry = (view.get("materials") or {}).get(str(material_id)) or {}
    if entry.get("name"):
        return str(entry["name"])
    return f"材料 {material_id}"


def _material_color(view: dict[str, Any], material_id: Any) -> str:
    entry = (view.get("materials") or {}).get(str(material_id)) or {}
    color = entry.get("color")
    if isinstance(color, str) and color.strip():
        return color
    try:
        index = abs(int(material_id)) % len(_MATERIAL_PALETTE)
    except (TypeError, ValueError):
        index = 0
    return _MATERIAL_PALETTE[index]


def _axis_indices(view: dict[str, Any], points: list[Any]) -> tuple[int, int]:
    """Resolve horizontal/vertical point indices from the snapshot axis map.

    ViennaPS 2D cross sections are authored on x/z, but some writers store the
    height in the ``h`` slot. Prefer ``axisMap.z`` as requested and fall back to
    ``axisMap.h`` when the z slot is degenerate, so a 2D view is never collapsed
    into a single horizontal line by an axis mismatch.
    """

    axis_map = view.get("axisMap") or {}
    try:
        x_index = int(axis_map.get("x", 0))
    except (TypeError, ValueError):
        x_index = 0
    try:
        z_index = int(axis_map.get("z", 2))
    except (TypeError, ValueError):
        z_index = 2
    try:
        h_index = int(axis_map.get("h", 1))
    except (TypeError, ValueError):
        h_index = 1

    def span(index: int) -> float:
        values = [float(point[index]) for point in points if len(point) > index]
        return (max(values) - min(values)) if values else 0.0

    vertical = z_index
    if view.get("dimension") != 3 and span(z_index) <= 1e-12 and span(h_index) > 1e-12:
        vertical = h_index
    if span(vertical) <= 1e-12 and span(h_index) > 1e-12:
        vertical = h_index
    return x_index, vertical


def _nice_step(span: float, target: int = 4) -> float:
    if not math.isfinite(span) or span <= 0:
        return 1.0
    raw = span / max(1, target)
    power = 10 ** math.floor(math.log10(raw))
    for multiplier in (1, 2, 2.5, 5, 10):
        if raw <= multiplier * power:
            return multiplier * power
    return 10 * power


def _tick_text(value: float, step: float) -> str:
    if step >= 1:
        return f"{value:.0f}"
    decimals = min(6, max(0, int(math.ceil(-math.log10(step)))))
    return f"{value:.{decimals}f}"


def _material_chains(edges: list[tuple[int, int]], limit: int = 4000) -> list[list[int]]:
    """Join one material's boundary edges into open or closed polylines.

    A GDS cross section shares boundaries between materials, so the contour of a
    single material is usually a chain, not a closed loop. The caller closes each
    chain down to the bottom of the plot to paint the region.
    """

    used = [False] * len(edges)
    adjacency: dict[int, list[int]] = {}
    for index, (a, b) in enumerate(edges):
        adjacency.setdefault(a, []).append(index)
        adjacency.setdefault(b, []).append(index)

    def other(edge_index: int, node: int) -> int:
        a, b = edges[edge_index]
        return b if a == node else a

    chains: list[list[int]] = []
    for start in range(len(edges)):
        if used[start]:
            continue
        used[start] = True
        chain = [edges[start][0], edges[start][1]]
        grew = True
        while grew and len(chain) <= limit:
            grew = False
            tail = chain[-1]
            for edge_index in adjacency.get(tail, []):
                if used[edge_index]:
                    continue
                used[edge_index] = True
                chain.append(other(edge_index, tail))
                grew = True
                break
        grew = True
        while grew and len(chain) <= limit:
            grew = False
            head = chain[0]
            for edge_index in adjacency.get(head, []):
                if used[edge_index]:
                    continue
                used[edge_index] = True
                chain.insert(0, other(edge_index, head))
                grew = True
                break
        chains.append(chain)
    return chains


def _svg_legend(parts: list[str], view: dict[str, Any], materials: list[int], x: float, y: float) -> None:
    materials = materials[:8]
    if not materials:
        return
    legend_width = 146
    legend_height = 18 + 16 * len(materials)
    parts.append(f'<rect x="{x:.3f}" y="{y:.3f}" width="{legend_width}" height="{legend_height}" fill="#ffffff" fill-opacity="0.92" stroke="#d8dee6"/>')
    parts.append(f'<text x="{x + 8:.3f}" y="{y + 13:.3f}" font-size="9" fill="#334155" font-weight="600">材料图例</text>')
    for row, material in enumerate(materials):
        item_y = y + 22 + row * 16
        color = _material_color(view, material)
        parts.append(f'<rect x="{x + 8:.3f}" y="{item_y - 8:.3f}" width="10" height="10" fill="{_escape(color)}" stroke="#94a3b8" stroke-width="0.5"/>')
        parts.append(f'<text x="{x + 24:.3f}" y="{item_y + 1:.3f}" font-size="9" fill="#17212b">{_escape(_material_name(view, material))}</text>')


def _svg_for_snapshot(snapshot: dict[str, Any], width: int = 560, height: int = 360) -> str:
    view = candidate_view(snapshot)
    points = view["points"]
    if not points:
        return '<p class="muted">快照没有可绘制的点。</p>'
    dimension = int(view.get("dimension") or 0)
    if dimension == 3 and view.get("triangles"):
        return _svg_isometric(view, width=width, height=height)
    return _svg_section(view, width=width, height=height)


def _svg_section(view: dict[str, Any], width: int = 560, height: int = 360) -> str:
    """2D cross section: material fills traced from lines + materialIds."""

    points = view["points"]
    dimension = int(view.get("dimension") or 0)
    x_index, y_index = _axis_indices(view, points)
    xs = [float(point[x_index]) for point in points if len(point) > x_index]
    ys = [float(point[y_index]) for point in points if len(point) > y_index]
    if not xs or not ys:
        return '<p class="muted">快照没有可绘制的坐标。</p>'
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    x_span = (x1 - x0) or 1.0
    y_span = (y1 - y0) or 1.0
    left, right, top, bottom = 62, 18, 18, 52
    plot_width = max(1.0, width - left - right)
    plot_height = max(1.0, height - top - bottom)
    scale = min(plot_width / x_span, plot_height / y_span)
    offset_x = left + (plot_width - x_span * scale) / 2.0
    offset_y = top + (plot_height - y_span * scale) / 2.0

    def px(value: float) -> float:
        return offset_x + (value - x0) * scale

    def py(value: float) -> float:
        return offset_y + (y1 - value) * scale

    fill_bottom = top + plot_height
    unit = str(view.get("coordinateUnit") or "um")
    unit_label = "µm" if unit in {"um", "µm"} else unit
    vertical_label = "z" if y_index == 2 and dimension == 3 else ("z" if y_index == 2 else "高度")
    aria = f"{dimension}D 结构视图：横轴 x，纵轴 {vertical_label}，单位 {unit_label}"
    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="report-svg" role="img" aria-label="{_escape(aria)}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fbfdff"/>',
        f'<rect x="{left}" y="{top}" width="{plot_width:.3f}" height="{plot_height:.3f}" fill="#ffffff" stroke="#d8dee6"/>',
    ]

    step = _nice_step(max(x_span, y_span) / 4)
    tick = math.ceil(x0 / step) * step
    while tick <= x1 + step * 1e-9:
        gx = px(tick)
        parts.append(f'<line x1="{gx:.3f}" y1="{top}" x2="{gx:.3f}" y2="{fill_bottom:.3f}" stroke="#eef2f7" stroke-width="1"/>')
        parts.append(f'<text x="{gx:.3f}" y="{fill_bottom + 14:.3f}" font-size="9" fill="#6b7785" text-anchor="middle">{_tick_text(tick, step)}</text>')
        tick += step
    tick = math.ceil(y0 / step) * step
    while tick <= y1 + step * 1e-9:
        gy = py(tick)
        parts.append(f'<line x1="{left}" y1="{gy:.3f}" x2="{left + plot_width:.3f}" y2="{gy:.3f}" stroke="#eef2f7" stroke-width="1"/>')
        parts.append(f'<text x="{left - 6:.3f}" y="{gy + 3:.3f}" font-size="9" fill="#6b7785" text-anchor="end">{_tick_text(tick, step)}</text>')
        tick += step

    parts.append(f'<line x1="{left}" y1="{fill_bottom:.3f}" x2="{left + plot_width:.3f}" y2="{fill_bottom:.3f}" stroke="#94a3b8" stroke-width="1.2"/>')
    parts.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{fill_bottom:.3f}" stroke="#94a3b8" stroke-width="1.2"/>')
    parts.append(f'<text x="{left + plot_width / 2:.3f}" y="{height - 10:.3f}" font-size="10" fill="#334155" text-anchor="middle">x ({_escape(unit_label)})</text>')
    parts.append(f'<text x="14" y="{top + plot_height / 2:.3f}" font-size="10" fill="#334155" text-anchor="middle" transform="rotate(-90 14 {top + plot_height / 2:.3f})">{_escape(vertical_label)} ({_escape(unit_label)})</text>')

    drawn: list[int] = []
    if dimension == 2:
        material_ids = view.get("lineMaterialIds") or []
        by_material: dict[int, list[tuple[int, int]]] = {}
        for index, line in enumerate(view.get("lines") or []):
            try:
                a, b = int(line[0]), int(line[1])
            except (IndexError, TypeError, ValueError):
                continue
            if a == b or a >= len(points) or b >= len(points):
                continue
            material = int(material_ids[index]) if index < len(material_ids) else -1
            by_material.setdefault(material, []).append((a, b))
        for material, edges in by_material.items():
            color = _material_color(view, material)
            for chain in _material_chains(edges)[:200]:
                chain_xs = [float(points[index][x_index]) for index in chain if index < len(points)]
                if not chain_xs:
                    continue
                closed = len(chain) > 2 and chain[0] == chain[-1]
                if not closed and (max(chain_xs) - min(chain_xs) < 1e-12):
                    continue
                coords = [(px(float(points[index][x_index])), py(float(points[index][y_index])))
                          for index in chain if index < len(points)]
                if not coords:
                    continue
                if not closed:
                    coords = coords + [(coords[-1][0], fill_bottom), (coords[0][0], fill_bottom)]
                points_text = " ".join(f"{cx:.3f},{cy:.3f}" for cx, cy in coords)
                parts.append(f'<polygon points="{points_text}" fill="{_escape(color)}" fill-opacity="0.32" stroke="{_escape(color)}" stroke-width="0.8"/>')
                if material not in drawn:
                    drawn.append(material)
            for a, b in edges:
                parts.append(
                    f'<line x1="{px(float(points[a][x_index])):.3f}" y1="{py(float(points[a][y_index])):.3f}" '
                    f'x2="{px(float(points[b][x_index])):.3f}" y2="{py(float(points[b][y_index])):.3f}" '
                    f'stroke="{_escape(color)}" stroke-width="1.1" stroke-opacity="0.9"/>'
                )
            if material not in drawn:
                drawn.append(material)
    else:
        # A 3D snapshot without triangles: fall back to a projected point cloud.
        for index, point in enumerate(points):
            material = view["materialIds"][index] if index < len(view["materialIds"]) else -1
            color = _material_color(view, material)
            parts.append(f'<circle cx="{px(float(point[x_index])):.3f}" cy="{py(float(point[y_index])):.3f}" r="1.1" fill="{_escape(color)}" fill-opacity="0.85"/>')
            if material not in drawn:
                drawn.append(material)

    bar_value = _nice_step(x_span / 4, 1)
    if bar_value * scale > plot_width * 0.6:
        bar_value = _nice_step(bar_value / 2, 1)
    bar_px = max(1.0, bar_value * scale)
    bar_x = left + plot_width - bar_px - 14
    bar_y = top + plot_height - 12
    parts.append(f'<line x1="{bar_x:.3f}" y1="{bar_y:.3f}" x2="{bar_x + bar_px:.3f}" y2="{bar_y:.3f}" stroke="#17212b" stroke-width="3"/>')
    parts.append(f'<line x1="{bar_x:.3f}" y1="{bar_y - 4:.3f}" x2="{bar_x:.3f}" y2="{bar_y + 4:.3f}" stroke="#17212b" stroke-width="1.2"/>')
    parts.append(f'<line x1="{bar_x + bar_px:.3f}" y1="{bar_y - 4:.3f}" x2="{bar_x + bar_px:.3f}" y2="{bar_y + 4:.3f}" stroke="#17212b" stroke-width="1.2"/>')
    parts.append(f'<text x="{bar_x + bar_px / 2:.3f}" y="{bar_y - 7:.3f}" font-size="9" fill="#17212b" text-anchor="middle">{_fmt_number(bar_value)} {_escape(unit_label)}</text>')

    # Legend lists only materials that were actually drawn.
    _svg_legend(parts, view, drawn, left + plot_width - 146 - 8, top + 8)

    parts.append("</svg>")
    return "".join(parts)


def _svg_isometric(view: dict[str, Any], width: int = 560, height: int = 360) -> str:
    """Isometric projection of a 3D snapshot (u = x - 0.5y, v = z + 0.35y)."""

    points = view["points"]
    axis_map = view.get("axisMap") or {}

    def axis_index(key: str, fallback: int) -> int:
        try:
            return int(axis_map.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    xi, yi, zi = axis_index("x", 0), axis_index("y", 1), axis_index("z", 2)
    projected: list[tuple[float, float, float]] = []
    raw: list[tuple[float, float, float]] = []
    for point in points:
        try:
            x, y, z = float(point[xi]), float(point[yi]), float(point[zi])
        except (IndexError, TypeError, ValueError):
            projected.append((0.0, 0.0, 0.0))
            raw.append((0.0, 0.0, 0.0))
            continue
        projected.append((x - 0.5 * y, z + 0.35 * y, y))
        raw.append((x, y, z))
    us = [row[0] for row in projected]
    vs = [row[1] for row in projected]
    u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
    u_span = (u1 - u0) or 1.0
    v_span = (v1 - v0) or 1.0
    left, right, top, bottom = 62, 18, 18, 52
    plot_width = max(1.0, width - left - right)
    plot_height = max(1.0, height - top - bottom)
    # Keep 8 % of the plot free on every side: the projected top face and the
    # hole opening used to touch (and sometimes cross) the frame.
    iso_padding = 0.08
    scale = min(plot_width / u_span, plot_height / v_span) * (1.0 - 2.0 * iso_padding)
    offset_x = left + (plot_width - u_span * scale) / 2.0
    offset_y = top + (plot_height - v_span * scale) / 2.0

    def px(value: float) -> float:
        return offset_x + (value - u0) * scale

    def py(value: float) -> float:
        return offset_y + (v1 - value) * scale

    unit = str(view.get("coordinateUnit") or "um")
    unit_label = "µm" if unit in {"um", "µm"} else unit
    aria = f"3D 结构等轴投影：u = x - 0.5y，v = z + 0.35y，单位 {unit_label}"
    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="report-svg" role="img" aria-label="{_escape(aria)}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fbfdff"/>',
        f'<rect x="{left}" y="{top}" width="{plot_width:.3f}" height="{plot_height:.3f}" fill="#ffffff" stroke="#d8dee6"/>',
    ]

    triangles = view.get("triangles") or []
    material_ids = view.get("triangleMaterialIds") or []
    faces: list[tuple[float, int, int, int, int]] = []
    for index, triangle in enumerate(triangles):
        try:
            a, b, c = int(triangle[0]), int(triangle[1]), int(triangle[2])
        except (IndexError, TypeError, ValueError):
            continue
        if max(a, b, c) >= len(projected):
            continue
        depth = (projected[a][2] + projected[b][2] + projected[c][2]) / 3.0
        faces.append((depth, index, a, b, c))
    # Far faces (larger y) first so nearer material covers them.
    faces.sort(key=lambda row: row[0], reverse=True)

    drawn: list[int] = []
    for _depth, index, a, b, c in faces:
        material = int(material_ids[index]) if index < len(material_ids) else -1
        color = _material_color(view, material)
        points_text = " ".join(
            f"{px(projected[vertex][0]):.3f},{py(projected[vertex][1]):.3f}" for vertex in (a, b, c)
        )
        parts.append(f'<polygon points="{points_text}" fill="{_escape(color)}" fill-opacity="0.88" stroke="{_escape(color)}" stroke-width="0.3"/>')
        if material not in drawn:
            drawn.append(material)

    if not faces:
        for index, row in enumerate(projected):
            material = view["materialIds"][index] if index < len(view["materialIds"]) else -1
            color = _material_color(view, material)
            parts.append(f'<circle cx="{px(row[0]):.3f}" cy="{py(row[1]):.3f}" r="1.2" fill="{_escape(color)}" fill-opacity="0.85"/>')
            if material not in drawn:
                drawn.append(material)

    # Axes: project the three unit directions from the minimum corner.
    x_min, y_min, z_min = min(r[0] for r in raw), min(r[1] for r in raw), min(r[2] for r in raw)
    x_max, y_max, z_max = max(r[0] for r in raw), max(r[1] for r in raw), max(r[2] for r in raw)
    origin = (x_min, y_min, z_min)

    def project_corner(x: float, y: float, z: float) -> tuple[float, float]:
        return px(x - 0.5 * y), py(z + 0.35 * y)

    ox, oy = project_corner(*origin)
    for end, label in (
        ((x_max, y_min, z_min), "x"),
        ((x_min, y_max, z_min), "y"),
        ((x_min, y_min, z_max), "z"),
    ):
        ex, ey = project_corner(*end)
        parts.append(f'<line x1="{ox:.3f}" y1="{oy:.3f}" x2="{ex:.3f}" y2="{ey:.3f}" stroke="#64748b" stroke-width="1.2"/>')
        parts.append(f'<text x="{ex + 4:.3f}" y="{ey - 2:.3f}" font-size="9" fill="#334155">{label}</text>')

    bar_value = _nice_step(max(u_span, v_span) / 4, 1)
    bar_px = max(1.0, bar_value * scale)
    bar_x = left + plot_width - bar_px - 14
    bar_y = top + plot_height - 12
    parts.append(f'<line x1="{bar_x:.3f}" y1="{bar_y:.3f}" x2="{bar_x + bar_px:.3f}" y2="{bar_y:.3f}" stroke="#17212b" stroke-width="3"/>')
    parts.append(f'<text x="{bar_x + bar_px / 2:.3f}" y="{bar_y - 7:.3f}" font-size="9" fill="#17212b" text-anchor="middle">{_fmt_number(bar_value)} {_escape(unit_label)}</text>')

    _svg_legend(parts, view, drawn, left + plot_width - 146 - 8, top + 8)
    parts.append("</svg>")
    return "".join(parts)


def _parameters_text(parameters: dict[str, Any], names: dict[str, str]) -> str:
    parts: list[str] = []
    for step_id, overrides in sorted((parameters or {}).items()):
        if not isinstance(overrides, dict):
            continue
        label = _step_label(step_id, names)
        for key, value in sorted(overrides.items()):
            unit = _PARAMETER_UNITS.get(str(key), "")
            parts.append(f"{label} {key}={_fmt_number(value)}{unit}")
    return " · ".join(parts) if parts else "未提供"


def _run_step_id(store: Any, run_id: str) -> str | None:
    """Last snapshot step of a run: what 03 shows by default for that Segment."""

    try:
        segment = store.get_segment_by_run_id(run_id)
    except Exception:
        segment = None
    if segment is not None and getattr(segment, "throughStepId", None):
        return str(segment.throughStepId)
    try:
        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
    except Exception:
        return None
    listed = manifest.get("snapshots") or []
    return str(listed[-1].get("stepId")) if listed and listed[-1].get("stepId") else None


def build_report(store: Any, request: dict[str, Any]) -> dict[str, Any]:
    if not any(request.get(key) for key in ("runId", "measurementId", "experimentId")):
        raise WorkflowValidation("报告需要至少一次运行、测量或实验")
    title = str(request.get("title") or "Workflow report")
    experiment = None
    if request.get("experimentId"):
        experiment = store.get_experiment(str(request["experimentId"]))
    run_id = request.get("runId")
    step_id = request.get("stepId")
    measurement = None
    provenance = None
    manifest = None
    route = None
    segment = None
    compile = None
    binding = None
    measurement_id = request.get("measurementId")
    replicate_ref = None
    # True when `run_id` was borrowed from an experiment replicate rather than
    # sent by 03: the report must not call an experiment candidate "当前 Run".
    run_from_experiment = False
    if experiment and not run_id:
        for variant in experiment.get("variants") or []:
            replicate = (variant.get("replicates") or [{}])[0]
            if replicate.get("runId"):
                run_id = replicate["runId"]
                replicate_ref = replicate
                measurement = replicate
                run_from_experiment = True
                break
    if run_id:
        run_dir = storage.run_path(str(run_id))
        manifest = storage.read_json(run_dir / "manifest.json")
        try:
            segment = store.get_segment_by_run_id(str(run_id))
        except Exception:
            segment = None
        if segment is not None:
            step_id = step_id or segment.throughStepId
            try:
                compile = store.get_compile(segment.sourceCompileId)
                route = store.get_plan(store.get_attempt(segment.attemptId).routeId)
            except Exception:
                pass
        if not step_id:
            listed = manifest.get("snapshots") or []
            step_id = listed[-1].get("stepId") if listed else None
    if experiment and not measurement:
        for variant in experiment.get("variants") or []:
            for replicate in variant.get("replicates") or []:
                if run_id and replicate.get("runId") != run_id:
                    continue
                if replicate.get("measurementId"):
                    measurement = replicate
                    break
            if measurement:
                break
    if measurement_id:
        measurement = measurement_id
    if measurement and isinstance(measurement, dict):
        measurement = measurement.get("measurementId")
    if isinstance(measurement, str):
        try:
            measurement = store.get_measurement(measurement)
        except Exception:
            measurement = None
    elif measurement is not None and not hasattr(measurement, "measurementId"):
        measurement = None
    if measurement is None and run_id:
        try:
            listed = store.list_measurements(run_id=str(run_id))
            measurement = listed[0] if listed else None
        except Exception:
            measurement = None
    if measurement is not None:
        try:
            provenance = store.list_verifications(measurement.measurementId)
        except Exception:
            provenance = None
    if experiment and experiment.get("baseCompileId"):
        try:
            compile = store.get_compile(experiment["baseCompileId"])
        except Exception:
            pass
    if experiment and experiment.get("binding"):
        binding = experiment["binding"]
    elif compile:
        try:
            binding = store.get_binding(compile.compileRecord.geometryBindingId).model_dump(mode="json", exclude_none=True)
        except Exception:
            binding = None

    step_names = _step_names(compile, manifest)
    sections: list[str] = []

    # -- 流程与编译 ---------------------------------------------------------
    sections.append("<h2>流程与编译</h2>")
    if compile:
        record = compile.compileRecord.model_dump(mode="json", exclude_none=True)
        # The main table carries names/status only; IDs and hashes stay in the
        # technical-details fold so the body stays readable.
        sections.append(_kv_table([
            ("流程", _display(record.get("flowId"))),
            ("种子策略", _token_label(record.get("seedPolicy"))),
        ]))
        sections.append(_details([
            ("编译 ID", _code(record.get("compileId"))),
            ("流程修订", _code(record.get("flowRevisionId"))),
            ("流程哈希", _code(record.get("flowRevisionHash"))),
            ("变量", _code(json.dumps(record.get("variableSet") or [], sort_keys=True))),
        ], summary="编译技术详情"))
        factors = ((experiment or {}).get("design") or {}).get("factors") or []
        if factors:
            sections.append("<h3>实验因子</h3>")
            sections.append(_table(
                ["步骤", "参数", "取值"],
                [[_escape(_step_label(factor.get("stepId"), step_names)), _escape(factor.get("parameter")),
                  _escape(json.dumps(factor.get("values") or []))] for factor in factors],
            ))
        sections.append("<h3>步骤</h3>")
        sections.append(_table(
            ["编号", "步骤", "路径"],
            [[str(index + 1), _escape(_step_label(entry.stepId, step_names)), _escape(entry.flowPath)]
             for index, entry in enumerate(compile.sourceMap)],
        ))
        sections.append(_details([
            ("步骤 ID / 作者步骤 ID", "<br>".join(
                "%s → %s" % (_code(entry.stepId), _code(entry.authoredStepId))
                for entry in compile.sourceMap) or "未记录"),
        ], summary="步骤 ID 技术详情"))
        sections.append(_details([
            ("编译版本", _display(record.get("compilerVersion"))),
            ("几何绑定哈希", _code(record.get("geometryBindingHash"))),
            ("完整工程哈希", _code(record.get("fullProjectHash"))),
            ("来源映射哈希", _code(record.get("sourceMapHash"))),
            ("步骤 ID 策略", _display(record.get("stepIdPolicy"))),
            ("材料定义哈希", _code(record.get("materialDefinitionsHash"))),
            ("引擎版本", _display(json.dumps(record.get("engineVersions") or {}, sort_keys=True))),
        ]))
    else:
        sections.append('<p class="muted">未选择编译记录。</p>')

    # -- 几何 / Clip --------------------------------------------------------
    sections.append("<h2>几何 / Clip</h2>")
    if binding:
        source = binding.get("geometrySource") or {}
        geometry = binding.get("geometry") or {}
        source_text = json.dumps(source, sort_keys=True) if source else "参数化几何"
        sections.append(_kv_table([
            ("几何类型", _display(geometry.get("kind"))),
            ("来源", _escape(source_text)),
        ]))
        sections.append(_details([
            ("绑定 ID", _code(binding.get("bindingId"))),
            ("来源结构", _display(source.get("structure"), "未提供")),
            ("多边形数量", _fmt_number(source.get("polygonCount"))),
            ("点数", _fmt_number(source.get("pointCount"))),
            ("域", _display(json.dumps(binding.get("domain") or {}, sort_keys=True))),
            ("网格", _display(json.dumps(binding.get("grid") or {}, sort_keys=True))),
        ]))
    else:
        sections.append('<p class="muted">未提供几何绑定。</p>')

    # -- 引擎与种子 --------------------------------------------------------
    sections.append("<h2>引擎与种子</h2>")
    if manifest:
        versions = manifest.get("versions") or {}
        solver = manifest.get("requestedSolver") or {}
        requested_random = solver.get("random") if isinstance(solver.get("random"), dict) else {}
        actual_seed = _number(manifest.get("actualSeed"))
        requested_seed = _number((requested_random or {}).get("seed"))
        seed_mode = (requested_random or {}).get("mode")
        if actual_seed is not None:
            seed_label, seed_value = "实际种子", _fmt_number(actual_seed)
        elif requested_seed is not None:
            seed_label, seed_value = "种子（请求值）", _fmt_number(requested_seed)
        else:
            seed_label, seed_value = "种子", "未记录（确定性 CPU 运行）"
        engine_text = "ViennaPS %s · ViennaLS %s" % (
            _display(versions.get("viennaps"), "未记录"), _display(versions.get("viennals"), "未记录"))
        sections.append(_kv_table([
            ("引擎", engine_text),
            ("请求引擎", _token_label(solver.get("enginePreference"))),
            (seed_label, seed_value),
            ("种子策略", _token_label(manifest.get("seedPolicy") or seed_mode)),
        ]))
        actual = manifest.get("actualExecution") or []
        if actual:
            sections.append("<h3>步骤引擎选择</h3>")
            sections.append(_table(
                ["步骤", "步骤引擎", "原因"],
                [[_escape(_step_label(row.get("stepId"), step_names)), _token_label(row.get("actualEngine")),
                  _escape(_engine_reason_text(row.get("engineSelectionReason")))] for row in actual[:40]],
            ))
        sections.append(_details([
            ("请求输入哈希", _code(manifest.get("requestedInputHash"))),
            ("执行哈希", _code(manifest.get("executionHash"))),
            ("运行模式", _display(manifest.get("runMode"))),
            ("维度", _fmt_number(manifest.get("dimension"))),
            ("应用版本", _display(versions.get("application"))),
            ("Python", _display(versions.get("python"))),
            ("计划执行哈希", _code(manifest.get("plannedExecutionHash"))),
            ("耗时（秒）", _fmt_number(manifest.get("elapsedSeconds"))),
        ]))
    else:
        sections.append('<p class="muted">未记录运行清单。</p>')

    # -- 运行与测量 --------------------------------------------------------
    sections.append("<h2>运行与测量</h2>")
    run_status = _status_label(manifest.get("status") if manifest else (getattr(segment, "status", None) if segment else None))
    run_route = route.route.name if route is not None and getattr(route, "route", None) is not None else None
    run_parts = ["运行 %s" % run_status]
    if manifest and (manifest.get("startedAt") or manifest.get("finishedAt")):
        run_parts.append("%s → %s" % (_display(manifest.get("startedAt")), _display(manifest.get("finishedAt"))))
    if run_route:
        run_parts.append("路由 %s" % _escape(run_route))
    if step_id:
        run_parts.append("步骤 %s" % _escape(_step_label(step_id, step_names)))
    sections.append("<p>%s</p>" % " · ".join(run_parts))
    sections.append(_details([
        ("运行 ID", _code(run_id)),
        ("步骤 ID", _code(step_id)),
        ("路由 ID", _code(getattr(getattr(route, "route", None), "routeId", None))),
        ("尝试 ID", _code(getattr(segment, "attemptId", None))),
        ("段状态", _status_label(getattr(segment, "status", None))),
        ("输入哈希", _code(getattr(segment, "provenanceInputHash", None))),
        ("执行哈希", _code(getattr(segment, "executionHash", None))),
        ("结果哈希", _code(getattr(segment, "resultDigest", None))),
    ]))
    if measurement is not None:
        metric_name = None
        try:
            metric_name = store.get_metric(measurement.metricId).name
        except Exception:
            metric_name = None
        metric_label = metric_name or measurement.metricId
        has_value = _measurement_has_value(measurement)
        value_text = _fmt_number(measurement.value) if has_value else "未记录"
        unit_text = _unit_text(measurement.unit) if has_value else ""
        status_text = _status_label(measurement.status)
        reason_text = _reason_text(measurement.reason)
        headline = "测量 %s · 数值 %s%s · %s" % (
            _escape(metric_label), value_text, (" " + unit_text) if unit_text else "", status_text)
        if not has_value:
            headline += " · 说明 %s" % _escape(reason_text)
        sections.append("<p>%s</p>" % headline)
        rows: list[tuple[str, str]] = [
            ("指标", _escape(metric_label)),
            ("配方", "%s v%s" % (_escape(measurement.recipeId), _display(measurement.recipeVersion, ""))),
            ("方法", "%s · %s" % (_escape(_method_label(measurement.method)), _display(measurement.material))),
            ("数值", value_text),
            ("状态", status_text),
        ]
        if unit_text:
            rows.append(("单位", unit_text))
        if not has_value:
            rows.append(("说明", _escape(reason_text)))
        rows.extend([
            ("绑定状态", _status_label(measurement.bindingStatus)),
            ("等价状态", _status_label(measurement.equivalenceStatus)),
        ])
        sections.append(_kv_table(rows))
        raw_reason = _reason_detail(measurement.reason)
        sections.append(_details([
            ("测量 ID", _code(measurement.measurementId)),
            ("来源快照", _code(measurement.sourceSnapshotHash)),
            ("算法版本", _display(measurement.algorithmVersion)),
            ("来源运行 ID", _code(measurement.sourceRunId)),
            ("来源步骤 ID", _code(measurement.sourceStepId)),
            ("来源状态 ID", _code(measurement.sourceStateId)),
            ("不确定度", _fmt_number(measurement.uncertainty)),
            ("不确定度状态", _status_label(measurement.uncertaintyStatus)),
            ("坐标系统", _display(measurement.coordinateSystem)),
            ("原因（原始）", _code(raw_reason) if raw_reason else "未提供"),
        ]))
        if provenance:
            sections.append("<h3>证据</h3>")
            sections.append(_table(
                ["证据", "状态", "方法版本"],
                [[_code(row.verificationId), _status_label(row.status), _display(row.methodVersion)] for row in provenance],
            ))
    else:
        sections.append('<p class="muted">未记录测量结果。</p>')

    # -- 实验与统计 --------------------------------------------------------
    if experiment:
        sections.append("<h2>实验与统计</h2>")
        condition_count = (experiment.get("design") or {}).get("conditionCount", len(experiment.get("variants") or []))
        sections.append(_kv_table([
            ("实验", _display(experiment.get("name"))),
            ("条件数", _fmt_number(condition_count)),
            ("状态", _status_label(experiment.get("status"))),
        ]))
        variants = experiment.get("variants") or []
        if variants:
            sections.append("<h3>候选</h3>")
            sections.append(_table(
                ["编号", "条件", "状态", "运行数"],
                [[_escape("V%s" % _fmt_number(variant.get("ordinal") or index + 1)),
                  _escape(_variant_title(variant, index)),
                  _escape(_variant_status(variant)), _fmt_number(len(variant.get("replicates") or []))]  # title = conditions + seed
                 for index, variant in enumerate(variants)],
            ))
        aggregation = experiment.get("aggregation") or {}
        rows = aggregation.get("rows") or []
        if rows:
            sections.append("<h3>统计汇总</h3>")
            sections.append(_table(
                ["编号", "参数", "样本数", "均值", "标准差", "95% 置信区间", "统计状态"],
                [[_fmt_number(row.get("ordinal")), _escape(_parameters_text(row.get("parameters") or {}, step_names)),
                  _fmt_number(row.get("n")), _fmt_number(row.get("mean")), _fmt_number(row.get("std")),
                  _fmt_number(row.get("ci95")), _status_label(row.get("statisticalStatus"))] for row in rows],
            ))
            sections.append(_details([
                ("CI 方法", _display(aggregation.get("ciMethod"))),
                ("单位", _unit_text(aggregation.get("unit"))),
                ("假设", _display(json.dumps(aggregation.get("assumptions") or [], ensure_ascii=False))),
                ("样本来源", _display(aggregation.get("sampleSource"))),
            ]))
        if request.get("lower") is not None and request.get("upper") is not None:
            window = process_window(experiment, float(request["lower"]), float(request["upper"]))
            unit = window.get("unit") or ""
            sections.append("<h3>工艺窗口</h3>")
            sections.append(_kv_table([
                ("工艺窗口", "%s – %s %s" % (_fmt_number(window.get("lower")), _fmt_number(window.get("upper")), _unit_text(unit))),
                ("候选", "%s 个" % _fmt_number(len(window.get("candidates") or []))),
            ]))
            sections.append("<p>通过 %s · 超出 %s · 不确定 %s · 未评估 %s</p>" % (
                _fmt_number(window["summary"]["pass"]), _fmt_number(window["summary"]["fail"]),
                _fmt_number(window["summary"]["uncertain"]), _fmt_number(window["summary"]["notEvaluated"])))
            if window.get("candidates"):
                sections.append(_table(
                    ["候选", "参数", "均值", "95% 置信区间", "样本数", "状态"],
                    [[_fmt_number(row.get("ordinal")), _escape(_parameters_text(row.get("parameters") or {}, step_names)),
                      _fmt_number(row.get("mean")), _fmt_number(row.get("ci95")), _fmt_number(row.get("n")),
                      _status_label(row.get("status"))] for row in window["candidates"]],
                ))
            sections.append(_details([
                ("因子窗口", _display(json.dumps(window.get("factorWindows") or [], ensure_ascii=False))),
                ("统计明细", _display(json.dumps(window.get("rows") or [], ensure_ascii=False))),
            ]))

    # -- 结构视图 ----------------------------------------------------------
    sections.append("<h2>结构视图</h2>")
    view_snapshots: list[tuple[dict[str, Any], str]] = []
    shown_runs: set[str] = set()
    # 03 hands over the current Run: its snapshot is the same picture the user
    # just looked at, so the report starts with it and labels it honestly.
    if run_id and step_id and not run_from_experiment:
        try:
            view_snapshots.append((load_snapshot(str(run_id), str(step_id)), "当前 Run · %s" % _step_label(step_id, step_names)))
            shown_runs.add(str(run_id))
        except Exception:
            pass
    if experiment:
        # Same snapshot as 03: the first replicate that actually ran, at the
        # last step of that run (not the step of some other run).
        for index, variant in enumerate((experiment.get("variants") or [])[:3]):
            replicate = next((row for row in variant.get("replicates") or [] if row.get("runId")), None)
            if not replicate:
                continue
            run_for_view = str(replicate["runId"])
            if run_for_view in shown_runs:
                continue
            step_for_view = _run_step_id(store, run_for_view)
            if not step_for_view:
                continue
            try:
                snapshot = load_snapshot(run_for_view, step_for_view)
            except Exception:
                continue
            ordinal = variant.get("ordinal") or index + 1
            caption_parts = ["实验候选 V%s" % _fmt_number(ordinal)]
            conditions = _variant_conditions(variant)
            if conditions:
                caption_parts.append(conditions)
            seed = _variant_seed(variant)
            if seed is not None:
                caption_parts.append(f"seed {_fmt_number(seed)}")
            view_snapshots.append((snapshot, " · ".join(caption_parts)))
            shown_runs.add(run_for_view)
    elif run_id and step_id and not view_snapshots:
        try:
            view_snapshots.append((load_snapshot(str(run_id), str(step_id)), "当前 Run · %s" % _step_label(step_id, step_names)))
        except Exception:
            pass
    if view_snapshots:
        sections.append('<div class="views">')
        for snapshot, caption in view_snapshots:
            sections.append("<figure><figcaption>%s</figcaption>%s</figure>" % (_escape(caption), _svg_for_snapshot(snapshot)))
        sections.append("</div>")
    else:
        sections.append('<p class="muted">未记录可用的结构快照。</p>')

    # -- 结论 --------------------------------------------------------------
    conclusions = request.get("conclusions") or ""
    sections.append("<h2>结论</h2>")
    if conclusions:
        sections.append("<p>%s</p>" % _escape(conclusions))
    else:
        sections.append('<p><span class="muted">未填写</span></p>')

    body = "".join(sections)
    created_at = _now()
    timestamp = _report_time()
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    summary_parts = []
    if run_id:
        summary_parts.append("1 次运行")
    if measurement is not None:
        summary_parts.append("1 次测量")
    if experiment:
        summary_parts.append("1 个实验 · %s 个候选" % _fmt_number(len(experiment.get("variants") or [])))
    summary = " · ".join(summary_parts) or "无数据"
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{_escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;margin:32px;line-height:1.5;color:#17212b}}h1{{margin-bottom:0}}.meta{{color:#6b7785;margin-top:4px}}h2{{margin-top:26px;border-bottom:1px solid #d8dee6;padding-bottom:4px}}h3{{margin-top:18px;font-size:15px}}table{{border-collapse:collapse;width:100%;margin:8px 0}}th,td{{border:1px solid #d8dee6;padding:5px 8px;text-align:left;font-size:12px;vertical-align:top}}th{{background:#f4f7fa}}code{{font-size:11px}}.muted{{color:#6b7785}}.views{{display:flex;gap:12px;flex-wrap:wrap}}figure{{margin:0;flex:1 1 320px}}figcaption{{font-size:11px;color:#6b7785}}.report-svg{{width:100%;height:auto;border:1px solid #d8dee6;background:#fbfdff}}details.tech{{margin:8px 0;font-size:12px}}details.tech summary{{cursor:pointer;color:#475569}}details.tech table{{margin-top:6px}}@media print{{body{{margin:0}}button{{display:none}}details.tech{{display:none}}}}
</style></head><body><h1>{_escape(title)}</h1>
<p class="meta">生成时间 {_escape(timestamp)} · 报告摘要 {_escape(summary)}</p>
<details class="tech"><summary>技术详情</summary><table>
<tr><th>报告哈希</th><td><code>{_escape(body_hash)}</code></td></tr>
<tr><th>生成时间</th><td>{_escape(timestamp)}</td></tr>
<tr><th>格式版本</th><td>1</td></tr>
</table></details>
<button onclick="window.print()">打印 / 保存为 PDF</button>{body}</body></html>"""
    digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
    return {"reportId": f"report_{digest[:16]}", "title": title, "html": document, "sha256": digest, "createdAt": created_at}


# Replicate failures are stored as English runner messages. The CSV is a
# user-facing artifact, so known ones get a Chinese sentence; anything else
# keeps the raw text instead of pretending there was no reason.
_REPLICATE_REASON_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^interrupted by server restart$"), "服务重启导致中断"),
    (re.compile(r"^waiting for an available run slot$"), "等待可用的运行槽位"),
    (re.compile(r"^segment finished with (\w+)$"), "Segment 运行结束：{0}"),
    (re.compile(r"^preparation failed: (.+)$"), "运行准备失败：{0}"),
    (re.compile(r"^start failed: (.+)$"), "运行启动失败：{0}"),
)


def _replicate_reason_text(reason: Any) -> str:
    if reason is None or (isinstance(reason, str) and not reason.strip()):
        return ""
    text = str(reason)
    for pattern, template in _REPLICATE_REASON_PATTERNS:
        match = pattern.match(text)
        if match:
            groups = match.groups()
            if groups and template.endswith("{0}"):
                return template.format(_status_label(groups[0]))
            return template
    # Known metrology codes become Chinese; an unknown runner message keeps its
    # own words instead of being reported as "测量无有效值".
    detail = _reason_detail(text)
    return detail if detail is not None else _reason_text(text)


def experiment_csv(document: dict[str, Any], store: Any = None) -> str:
    """Experiment CSV with the same names/units/precision as the UI and report.

    `status` and `reason` are Chinese, numbers keep 4 significant digits, the
    unit is `µm`, and a replicate without a valid metric leaves value and unit
    empty instead of writing a placeholder number.
    """

    import csv
    import io

    from .experiments import experiment_table

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "experiment_id", "variant_id", "ordinal", "parameters", "replicate_id", "seed",
        "status", "reason", "value", "unit", "run_id", "statistical_sample", "attempt_count",
        "n", "mean", "std", "ci95", "min", "max",
    ])
    table = experiment_table(document)
    by_variant = {row["variantId"]: row for row in table["rows"]}
    for variant in document.get("variants") or []:
        summary = by_variant.get(variant.get("variantId"), {})
        for replicate in variant.get("replicates") or []:
            measurement = None
            if store is not None and replicate.get("measurementId"):
                try:
                    measurement = store.get_measurement(str(replicate["measurementId"]))
                except Exception:
                    measurement = None
            if measurement is not None:
                has_value = _measurement_has_value(measurement)
                value = measurement.value if has_value else None
                unit = "µm" if has_value else ""
                reason = "" if has_value else _reason_text(measurement.reason)
            else:
                value = replicate.get("value") if _number(replicate.get("value")) is not None else None
                has_value = value is not None and str(replicate.get("status")) == "succeeded"
                unit = "µm" if has_value else ""
                reason = "" if has_value else _replicate_reason_text(replicate.get("reason"))
            writer.writerow([
                document.get("experimentId"), variant.get("variantId"), variant.get("ordinal"),
                json.dumps(variant.get("parameters") or {}, sort_keys=True, separators=(",", ":")),
                replicate.get("replicateId"), replicate.get("seed"),
                _status_label(replicate.get("status")), reason,
                _sig4(value), unit, replicate.get("runId"),
                bool(replicate.get("statisticalSample")), int(replicate.get("attemptCount") or 0),
                summary.get("n"), _sig4(summary.get("mean")), _sig4(summary.get("std")),
                _sig4(summary.get("ci95")), _sig4(summary.get("min")), _sig4(summary.get("max")),
            ])
    return buffer.getvalue()
