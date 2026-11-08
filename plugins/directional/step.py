"""Worker step adapter for the native directional plugin.

A directional step carries ``model="directional"`` plus exactly one of the
three native shapes: scalar ``direction``/rate fields, ``rateSets``, or a
``materialRates`` table. :func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.directional.params.DirectionalParams` for a supported step and
``None`` for every other step.

Steps that carry a key this plugin does not understand return ``None`` so the
caller keeps the existing worker path instead of silently ignoring input.
Malformed values raise ``ValueError``.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import DirectionalMaterialRate, DirectionalParams, DirectionalRateSet

__all__ = ["parse_step"]

_ALLOWED_KEYS = frozenset(
    {
        "id",
        "name",
        "enabled",
        "operation",
        "model",
        "duration",
        "direction",
        "directionalRateMagnitude",
        "isotropicRateMagnitude",
        "calculateVisibility",
        "rateSets",
        "materialRates",
        "defaultMaterialRate",
        "rateSigns",
        "advection",
        "depositMaterial",
    }
)
_RATE_SET_KEYS = frozenset(
    {"direction", "directionalVelocity", "isotropicVelocity", "maskMaterials", "calculateVisibility"}
)
_MATERIAL_RATE_KEYS = frozenset({"directional", "isotropic"})


def _number(value: Any, path: str) -> float:
    """Return ``value`` as a finite float or raise ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite JSON number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{path} must be finite, got {value!r}")
    return number


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a JSON boolean, got {value!r}")
    return value


def _direction(value: Any, path: str) -> list[float]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{path} must be a 3-component JSON array")
    if len(value) != 3:
        raise ValueError(f"{path} must have exactly 3 components, got {len(value)}")
    return [_number(component, f"{path}[{index}]") for index, component in enumerate(value)]


def _mask_materials(value: Any, path: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{path} must be a JSON array or null")
    names: list[str] = []
    for index, name in enumerate(value):
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path}[{index}] must be a non-empty material name")
        names.append(name)
    return names


def _rate_set(value: Any, path: str) -> DirectionalRateSet:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a JSON object")
    unknown = sorted(set(value) - _RATE_SET_KEYS)
    if unknown:
        raise ValueError(f"{path} has unsupported keys: {', '.join(unknown)}")
    if "direction" not in value:
        raise ValueError(f"{path} requires direction")
    if "directionalVelocity" not in value:
        raise ValueError(f"{path} requires directionalVelocity")
    values: dict[str, Any] = {
        "direction": _direction(value["direction"], f"{path}.direction"),
        "directionalVelocity": _number(value["directionalVelocity"], f"{path}.directionalVelocity"),
    }
    if "isotropicVelocity" in value:
        values["isotropicVelocity"] = _number(value["isotropicVelocity"], f"{path}.isotropicVelocity")
    if "maskMaterials" in value:
        values["maskMaterials"] = _mask_materials(value["maskMaterials"], f"{path}.maskMaterials")
    if "calculateVisibility" in value:
        values["calculateVisibility"] = _boolean(value["calculateVisibility"], f"{path}.calculateVisibility")
    return DirectionalRateSet(**values)


def _material_rate(value: Any, path: str) -> DirectionalMaterialRate:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a JSON object with directional and isotropic rates")
    unknown = sorted(set(value) - _MATERIAL_RATE_KEYS)
    if unknown:
        raise ValueError(f"{path} has unsupported keys: {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for name in ("directional", "isotropic"):
        if name in value:
            values[name] = _number(value[name], f"{path}.{name}")
    return DirectionalMaterialRate(**values)


def _material_rates(value: Any, path: str) -> dict[str, DirectionalMaterialRate]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a JSON object")
    rates: dict[str, DirectionalMaterialRate] = {}
    for name, row in value.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path} keys must be non-empty material names")
        rates[name] = _material_rate(row, f"{path}.{name}")
    return rates


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> DirectionalParams | None:
    """Return validated params when ``step`` is a supported directional step.

    ``project`` is accepted for the common ``(project, step)`` hook signature;
    nothing in the project switches dispatch today. Unknown top-level keys make
    the step unsupported (``None``); malformed values of a known key raise
    ``ValueError``.
    """
    del project
    if step.get("model") != "directional":
        return None
    unknown = sorted(set(step) - _ALLOWED_KEYS)
    if unknown:
        return None

    operation = step.get("operation")
    if operation not in ("etch", "deposit"):
        raise ValueError("directional step requires operation 'etch' or 'deposit'")

    if "duration" not in step or step["duration"] is None:
        raise ValueError("directional step requires duration")

    values: dict[str, Any] = {
        "operation": operation,
        "duration": _number(step["duration"], "duration"),
    }
    if "direction" in step:
        values["direction"] = None if step["direction"] is None else _direction(step["direction"], "direction")
    for name in ("directionalRateMagnitude", "isotropicRateMagnitude"):
        if name in step:
            values[name] = None if step[name] is None else _number(step[name], name)
    if "calculateVisibility" in step:
        values["calculateVisibility"] = _boolean(step["calculateVisibility"], "calculateVisibility")

    if "rateSets" in step:
        raw_sets = step["rateSets"]
        if raw_sets is None:
            values["rateSets"] = None
        else:
            if not isinstance(raw_sets, (list, tuple)):
                raise ValueError("rateSets must be a JSON array or null")
            values["rateSets"] = [_rate_set(raw, f"rateSets[{index}]") for index, raw in enumerate(raw_sets)]
    if "materialRates" in step:
        raw_rates = step["materialRates"]
        values["materialRates"] = None if raw_rates is None else _material_rates(raw_rates, "materialRates")
    if "defaultMaterialRate" in step:
        raw_default = step["defaultMaterialRate"]
        values["defaultMaterialRate"] = (
            None if raw_default is None else _material_rate(raw_default, "defaultMaterialRate")
        )
    if "rateSigns" in step:
        raw_signs = step["rateSigns"]
        if raw_signs is not None and raw_signs not in ("operation", "native"):
            raise ValueError("rateSigns must be 'operation', 'native' or null")
        values["rateSigns"] = raw_signs
    if "advection" in step:
        raw_advection = step["advection"]
        if raw_advection is not None and not isinstance(raw_advection, Mapping):
            raise ValueError("advection must be a JSON object or null")
        values["advection"] = None if raw_advection is None else dict(raw_advection)
    if "depositMaterial" in step:
        raw_deposit = step["depositMaterial"]
        if raw_deposit is not None and (not isinstance(raw_deposit, str) or not raw_deposit):
            raise ValueError("depositMaterial must be a non-empty material name or null")
        values["depositMaterial"] = raw_deposit

    return DirectionalParams(**values)
