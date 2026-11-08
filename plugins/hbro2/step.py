"""Worker step adapter for the native HBr/O2 plugin.

A raw worker step for this process carries ``model="specialized"``,
``nativeModel="hbro2"``, a flat ``nativeParameters`` object, and ``duration``.
:func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.hbro2.params.HBrO2Params` for such a step and ``None`` for
every other step. Malformed values raise ``ValueError``.

``nativeParameters.advanced`` and any key outside the flat allowlist return
``None``: the flat overload is the only path this plugin implements, so the
caller keeps the old worker path for advanced parameter objects instead of
silently running a different simulation. This matters more for HBrO2 than for
SF6/O2 because the advanced defaults diverge from the flat constructor's.
``coverageTolerance`` is the one non-flat key that is accepted, because the
worker applies it as a separate ``CoverageParameters`` object and the plugin
does the same.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import FLAT_FIELDS, HBrO2Params

__all__ = ["parse_step"]

_OPTIONAL_KEYS = frozenset({"coverageTolerance"})
_ALLOWED_KEYS = frozenset(FLAT_FIELDS) | _OPTIONAL_KEYS


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


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> HBrO2Params | None:
    """Return validated params when ``step`` is a flat HBr/O2 etch step.

    ``project`` is accepted for the common ``(project, step)`` hook signature;
    nothing in the project switches dispatch today. A step that claims to be
    ``specialized``/``hbro2`` but lacks ``duration`` or carries an invalid flat
    value raises ``ValueError`` instead of being skipped.
    """
    del project
    if step.get("model") != "specialized":
        return None
    if step.get("nativeModel") != "hbro2":
        return None

    raw = step.get("nativeParameters")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("hbro2 nativeParameters must be a JSON object")

    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        # ``advanced`` lands here too: the parameter-object overload is not
        # implemented by this plugin, so leave the step to the worker path.
        return None

    duration = step.get("duration")
    if duration is None:
        raise ValueError("hbro2 step requires duration")

    values: dict[str, Any] = {"duration": _number(duration, "duration")}
    for name in FLAT_FIELDS:
        if name in raw:
            values[name] = _number(raw[name], f"nativeParameters.{name}")
    if "coverageTolerance" in raw:
        values["coverageTolerance"] = _number(raw["coverageTolerance"], "nativeParameters.coverageTolerance")

    return HBrO2Params(**values)
