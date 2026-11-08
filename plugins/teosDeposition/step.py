"""Worker step adapter for the native TEOS deposition plugin.

A raw worker step for this process carries ``model="specialized"``,
``nativeModel="teosDeposition"``, ``operation="deposit"``, a flat
``nativeParameters`` object, and ``duration``.
:func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.teosDeposition.params.TEOSDepositionParams` for such a step and
``None`` for every other step. Malformed values raise ``ValueError``.

``TEOSDeposition`` always grows SiO2, so a step-level ``depositMaterial`` is
accepted only when it is exactly ``"SiO2"``; any other value means some other
deposition path owns the step. ``advanced`` and any key outside the flat
allowlist also return ``None``. There is no ``coverageTolerance`` for this
model.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import FLAT_FIELDS, TEOSDepositionParams

__all__ = ["parse_step"]

_ALLOWED_KEYS = frozenset(FLAT_FIELDS)
_DEPOSIT_MATERIAL = "SiO2"


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


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> TEOSDepositionParams | None:
    """Return validated params when ``step`` is a flat TEOS deposition step.

    ``project`` is accepted for the common ``(project, step)`` hook signature;
    nothing in the project switches dispatch today. A step that claims to be
    ``specialized``/``teosDeposition`` but lacks ``duration`` or carries an
    invalid flat value raises ``ValueError`` instead of being skipped.
    """
    del project
    if step.get("model") != "specialized":
        return None
    if step.get("nativeModel") != "teosDeposition":
        return None
    if step.get("operation") != "deposit":
        return None
    if "depositMaterial" in step and step.get("depositMaterial") != _DEPOSIT_MATERIAL:
        return None

    raw = step.get("nativeParameters")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("teosDeposition nativeParameters must be a JSON object")

    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        # ``advanced`` lands here too: the parameter-object overload is not
        # implemented by this plugin, so leave the step to another path.
        return None

    duration = step.get("duration")
    if duration is None:
        raise ValueError("teosDeposition step requires duration")

    values: dict[str, Any] = {"duration": _number(duration, "duration")}
    for name in FLAT_FIELDS:
        if name in raw:
            values[name] = _number(raw[name], f"nativeParameters.{name}")

    return TEOSDepositionParams(**values)
