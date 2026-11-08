"""Worker step adapter for the native TEOS PECVD plugin.

A raw worker step for this process carries ``model="specialized"``,
``nativeModel="teospecvd"``, ``operation="deposit"``, a flat
``nativeParameters`` object, and ``duration``. :func:`parse_step` is the
plugin's declared :class:`~process_models.v2.contract.ProcessSpec` hook: it
returns :class:`~plugins.teospecvd.params.TEOSPECVDParams` for such a step and
``None`` for every other step. Malformed values raise ``ValueError``.

``nativeParameters.advanced`` and any key outside the flat allowlist return
``None``: the flat overload is the only path this plugin implements, so the
caller keeps the old worker path for advanced parameter objects instead of
silently running a different simulation. The native model always deposits SiO2,
so an explicit ``depositMaterial`` may only repeat that value; anything else is
not this process.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import FLAT_FIELDS, TEOSPECVDParams

__all__ = ["parse_step"]

_DEPOSIT_MATERIAL = "SiO2"
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


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> TEOSPECVDParams | None:
    """Return validated params when ``step`` is a flat TEOS PECVD step.

    ``project`` is accepted for the common ``(project, step)`` hook signature;
    nothing in the project switches dispatch today. A step that claims to be
    ``specialized``/``teospecvd``/``deposit`` but lacks ``duration`` or carries
    an invalid flat value raises ``ValueError`` instead of being skipped.
    """
    del project
    if step.get("model") != "specialized":
        return None
    if step.get("nativeModel") != "teospecvd":
        return None
    if step.get("operation") != "deposit":
        return None
    if "depositMaterial" in step and step.get("depositMaterial") != _DEPOSIT_MATERIAL:
        return None

    raw = step.get("nativeParameters")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("teospecvd nativeParameters must be a JSON object")

    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        # ``advanced`` lands here too: the parameter-object overload is not
        # implemented by this plugin, so leave the step to the worker path.
        return None

    duration = step.get("duration")
    if duration is None:
        raise ValueError("teospecvd step requires duration")

    values: dict[str, Any] = {"duration": _number(duration, "duration")}
    for name in FLAT_FIELDS:
        if name in raw:
            values[name] = _number(raw[name], f"nativeParameters.{name}")

    return TEOSPECVDParams(**values)
