"""Worker step adapter for the native CSV rate-grid process."""

from __future__ import annotations

from typing import Any, Mapping

from .params import CsvProcessParams

__all__ = ["parse_step"]

_ALLOWED_KEYS = frozenset(
    {
        "assetId",
        "fileName",
        "direction",
        "offset",
        "isotropicComponent",
        "directionalComponent",
        "interpolationMode",
        "idwNeighbors",
        "calculateVisibility",
    }
)


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite JSON number, got {value!r}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{path} must be finite, got {value!r}")
    return number


def _vector(value: Any, path: str, length: int) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{path} must contain exactly {length} numbers, got {value!r}")
    return [_number(item, f"{path}[{index}]") for index, item in enumerate(value)]


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> CsvProcessParams | None:
    if step.get("model") != "csvProcess":
        return None
    if step.get("operation") != "native":
        return None
    unknown = sorted(set(step) - _ALLOWED_KEYS - {"id", "name", "enabled", "model", "operation", "duration"})
    if unknown:
        return None
    duration = step.get("duration")
    if duration is None:
        raise ValueError("csvProcess step requires duration")
    asset_id = step.get("assetId")
    file_name = step.get("fileName")
    if not isinstance(asset_id, str) or not isinstance(file_name, str):
        raise ValueError("csvProcess step requires assetId and fileName strings")
    values: dict[str, Any] = {
        "duration": _number(duration, "duration"),
        "assetId": asset_id,
        "fileName": file_name,
        "direction": _vector(step.get("direction"), "direction", 3),
        "offset": _vector(step.get("offset"), "offset", 2),
        "interpolationMode": step.get("interpolationMode"),
        "calculateVisibility": step.get("calculateVisibility", True),
    }
    for name in ("isotropicComponent", "directionalComponent"):
        if name in step:
            values[name] = _number(step[name], name)
    if "idwNeighbors" in step:
        values["idwNeighbors"] = step["idwNeighbors"]
    return CsvProcessParams(**values)
