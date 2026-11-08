"""Worker step adapter for the native Faraday-cage plugin.

A raw worker step for this process carries ``model="specialized"``,
``nativeModel="faradayCage"``, a ``nativeParameters`` object, and ``duration``.
:func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.faradayCage.params.FaradayCageParams` for such a step and
``None`` for every other step. Malformed values raise ``ValueError``.

``nativeParameters.advanced`` and any key outside the scalar/material-table
allowlist return ``None``: the ``FaradayCageParameters`` path is the only one
this plugin implements, so the caller keeps the old worker path for advanced
parameter objects instead of silently running a different simulation.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import MATERIAL_RATE_FIELD, SCALAR_FIELDS, FaradayCageParams

__all__ = ["parse_step"]

_ALLOWED_KEYS = frozenset(SCALAR_FIELDS) | {MATERIAL_RATE_FIELD}


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


def _material_rates(value: Any) -> dict[str, float]:
    """Return the material-rate table as finite floats or raise ``ValueError``."""
    if not isinstance(value, Mapping):
        raise ValueError(f"nativeParameters.{MATERIAL_RATE_FIELD} must be a JSON object")
    rates: dict[str, float] = {}
    for name, rate in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"nativeParameters.{MATERIAL_RATE_FIELD} keys must be non-empty material names")
        rates[name] = _number(rate, f"nativeParameters.{MATERIAL_RATE_FIELD}.{name}")
    return rates


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> FaradayCageParams | None:
    """Return validated params when ``step`` is a Faraday-cage etch step.

    ``project`` is accepted for the common ``(project, step)`` hook signature;
    nothing in the project switches dispatch today. A step that claims to be
    ``specialized``/``faradayCage`` but lacks ``duration`` or carries an invalid
    value raises ``ValueError`` instead of being skipped.
    """
    del project
    if step.get("model") != "specialized":
        return None
    if step.get("nativeModel") != "faradayCage":
        return None

    raw = step.get("nativeParameters")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("faradayCage nativeParameters must be a JSON object")

    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        # ``advanced`` lands here too: the parameter-object overload is not
        # implemented by this plugin, so leave the step to the worker path.
        return None

    duration = step.get("duration")
    if duration is None:
        raise ValueError("faradayCage step requires duration")

    values: dict[str, Any] = {"duration": _number(duration, "duration")}
    for name in SCALAR_FIELDS:
        if name in raw:
            values[name] = _number(raw[name], f"nativeParameters.{name}")
    if MATERIAL_RATE_FIELD in raw:
        values[MATERIAL_RATE_FIELD] = _material_rates(raw[MATERIAL_RATE_FIELD])

    # Pydantic raises ValidationError (a ValueError subclass) for finite,
    # range, and thresholdEnergy < meanEnergy violations.
    return FaradayCageParams(**values)
