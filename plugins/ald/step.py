"""Worker step adapter for the native single-particle ALD plugin.

A raw worker step for this process carries ``model="specialized"``,
``nativeModel="ald"``, ``operation="deposit"``, a ``depositMaterial``, a flat
twelve-field ``nativeParameters`` object, and ``duration``.
:func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.ald.params.ALDParams` for such a step and ``None`` for every
other step. Malformed values raise ``ValueError``.

``nativeParameters.advanced`` and any key outside the flat allowlist return
``None``: the flat parameter-object path is the only one this plugin
implements, so the caller keeps the old worker path instead of silently
running a different simulation.

``surfaceDiffusion`` also returns ``None``, even when the rest of the step is
valid. The 4.7.0 native surface-diffusion stencil has a known hang, so that
object must keep flowing through the old worker path, which owns the capability
gate for it. This slice deliberately does not implement surface diffusion.

A step with ``depositMaterial`` missing or empty is *this* process with an
invalid field, so it raises ``ValueError`` instead of being skipped.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import ATOMIC_FIELDS, FLAT_FIELDS, NUM_CYCLES_FIELD, ALDParams

__all__ = ["parse_step"]

_FLAT_AND_ATOMIC = FLAT_FIELDS + ATOMIC_FIELDS
_ALLOWED_KEYS = frozenset(_FLAT_AND_ATOMIC) | {NUM_CYCLES_FIELD}
_SUPPORTED_DIMENSIONS = (2, 3)


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


def _integer(value: Any, path: str) -> int:
    """Return ``value`` as an ``int`` or raise ``ValueError``.

    JSON has one number type, so an integral ``2.0`` is accepted and stored as
    ``2``; a fractional, non-finite, boolean or string value is rejected
    instead of being truncated.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a JSON integer, got {value!r}")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{path} must be an integer, got {value!r}")
        return int(value)
    return int(value)


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> ALDParams | None:
    """Return validated params when ``step`` is a flat single-particle ALD step.

    ``None`` means the step is not this process (other model/native
    model/operation, unsupported dimension, ``surfaceDiffusion``, a top-level
    ``advanced`` object, or a ``nativeParameters`` key outside the flat
    allowlist). A step that claims to be ``specialized``/``ald``/``deposit`` but
    lacks ``duration``/``depositMaterial`` or carries an invalid flat value
    raises ``ValueError``.
    """
    if step.get("model") != "specialized":
        return None
    if step.get("nativeModel") != "ald":
        return None
    if step.get("operation") != "deposit":
        return None
    if project.get("dimension") not in (None, *_SUPPORTED_DIMENSIONS):
        return None
    # Keep the known-hanging 4.7.0 stencil on the old worker path, which owns
    # the capability gate.
    if step.get("surfaceDiffusion") is not None:
        return None
    if step.get("advanced") is not None:
        return None

    raw = step.get("nativeParameters")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("ald nativeParameters must be a JSON object")

    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        # ``nativeParameters.advanced`` lands here too: the parameter-object
        # overload is not implemented by this plugin, so leave the step to the
        # worker path.
        return None

    deposit_material = step.get("depositMaterial")
    if not isinstance(deposit_material, str) or not deposit_material.strip():
        raise ValueError("ald step requires a non-empty depositMaterial")

    duration = step.get("duration")
    if duration is None:
        raise ValueError("ald step requires duration")

    values: dict[str, Any] = {
        "duration": _number(duration, "duration"),
        "depositMaterial": deposit_material,
    }
    for name in _FLAT_AND_ATOMIC:
        if name in raw:
            values[name] = _number(raw[name], f"nativeParameters.{name}")
    if NUM_CYCLES_FIELD in raw:
        values[NUM_CYCLES_FIELD] = _integer(raw[NUM_CYCLES_FIELD], f"nativeParameters.{NUM_CYCLES_FIELD}")

    return ALDParams(**values)
