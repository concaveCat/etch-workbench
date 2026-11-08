"""Worker step adapter for the native crystalline wet-etch plugin.

A raw worker step for this process carries ``model="specialized"``,
``nativeModel="wetEtch"``, a flat ``nativeParameters`` object, and ``duration``.
:func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.wetEtch.params.WetEtchParams` for such a step and ``None`` for
every other step. Malformed values raise ``ValueError``.

``nativeParameters.advanced`` and any key outside the flat allowlist return
``None``: the flat overload is the only path this plugin implements, so the
caller keeps the old worker path for advanced parameter objects instead of
silently running a different simulation.

The ``materialRates`` table is authoritative and must be non-empty for this
plugin to claim a step. A step without it (or with an explicit empty table) is
left to the worker path, which validates the exact material coverage.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import FLAT_FIELDS, RATE_FIELDS, WetEtchParams

__all__ = ["parse_step"]

_ALLOWED_KEYS = frozenset(FLAT_FIELDS)


def _number(value: Any, path: str) -> float:
    """Return ``value`` as a finite float or raise ``ValueError``.

    Booleans and strings are rejected on purpose: the project schema only
    carries JSON numbers here, and coercing a string would change what the
    native constructor receives.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite JSON number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{path} must be finite, got {value!r}")
    return number


def _boolean(value: Any, path: str) -> bool:
    """Return ``value`` when it is a JSON boolean or raise ``ValueError``."""
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a JSON boolean, got {value!r}")
    return value


def _direction(value: Any, path: str) -> list[float]:
    """Return a validated three-component direction vector."""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{path} must be a three-component list, got {value!r}")
    if len(value) != 3:
        raise ValueError(f"{path} must contain exactly three numbers, got {value!r}")
    numbers = [_number(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if all(number == 0.0 for number in numbers):
        raise ValueError(f"{path} must not be the zero vector")
    return numbers


def _material_rates(value: Any, path: str) -> dict[str, float]:
    """Return the validated material-rate table from a JSON object."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a JSON object, got {value!r}")
    rates: dict[str, float] = {}
    for name, rate in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path} keys must be non-empty material names, got {name!r}")
        rates[name] = _number(rate, f"{path}.{name}")
    return rates


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> WetEtchParams | None:
    """Return validated params when ``step`` is a flat wet-etch step.

    ``project`` is accepted for the common ``(project, step)`` hook signature;
    nothing in the project switches dispatch today. A step that claims to be
    ``specialized``/``wetEtch`` but lacks ``duration`` or carries an invalid
    flat value raises ``ValueError`` instead of being skipped.
    """
    del project
    if step.get("model") != "specialized":
        return None
    if step.get("nativeModel") != "wetEtch":
        return None

    raw = step.get("nativeParameters")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("wetEtch nativeParameters must be a JSON object")

    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        # ``advanced`` lands here too: the parameter-object overload is not
        # implemented by this plugin, so leave the step to the worker path.
        return None

    if "materialRates" not in raw:
        # The material-rate table is authoritative; without it this plugin
        # cannot build a meaningful native model. The worker path validates
        # the exact material coverage and rejects the step there.
        return None
    rates = _material_rates(raw["materialRates"], "nativeParameters.materialRates")
    if not rates:
        return None

    duration = step.get("duration")
    if duration is None:
        raise ValueError("wetEtch step requires duration")

    values: dict[str, Any] = {
        "duration": _number(duration, "duration"),
        "materialRates": rates,
    }
    if "useCrystallineRates" in raw:
        values["useCrystallineRates"] = _boolean(raw["useCrystallineRates"], "nativeParameters.useCrystallineRates")
    for name in ("direction100", "direction010"):
        if name in raw:
            values[name] = _direction(raw[name], f"nativeParameters.{name}")
    for name in RATE_FIELDS:
        if name in raw:
            values[name] = _number(raw[name], f"nativeParameters.{name}")

    return WetEtchParams(**values)
