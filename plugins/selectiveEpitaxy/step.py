"""Worker step adapter for the native selective-epitaxy plugin.

A raw worker step for this process carries ``model="specialized"``,
``nativeModel="selectiveEpitaxy"``, ``operation="deposit"``, a
``nativeParameters`` object with a non-empty ``materialRates`` table, and
``duration``. :func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.selectiveEpitaxy.params.SelectiveEpitaxyParams` for such a step
and ``None`` for every other step. Malformed values raise ``ValueError``.

``SelectiveEpitaxy`` always grows SiGe, so a step-level ``depositMaterial`` is
accepted only when it is exactly ``"SiGe"``; any other value means some other
deposition path owns the step. ``advanced`` and any key outside the allowlist
also return ``None``. The optional ``nvFactors`` three-vector selects the
separate native constructor overload and is accepted verbatim.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import OPTIONAL_FIELDS, SelectiveEpitaxyParams, material_rates, nv_factors

__all__ = ["parse_step"]

_ALLOWED_KEYS = frozenset({"materialRates", "rate111", "rate100"}) | frozenset(OPTIONAL_FIELDS)
_DEPOSIT_MATERIAL = "SiGe"


def _number(value: Any, path: str) -> float:
    """Return ``value`` as a finite float or raise ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite JSON number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{path} must be finite, got {value!r}")
    return number


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> SelectiveEpitaxyParams | None:
    """Return validated params when ``step`` is a selective-epitaxy step.

    ``project`` is accepted for the common ``(project, step)`` hook signature;
    nothing in the project switches dispatch today. A step that claims to be
    ``specialized``/``selectiveEpitaxy`` but lacks ``duration`` or
    ``materialRates``, or carries an invalid value, raises ``ValueError``
    instead of being skipped: the plugin is the only v2 path for this native
    model.
    """
    del project
    if step.get("model") != "specialized":
        return None
    if step.get("nativeModel") != "selectiveEpitaxy":
        return None
    if step.get("operation") != "deposit":
        return None
    if "depositMaterial" in step and step.get("depositMaterial") != _DEPOSIT_MATERIAL:
        return None

    raw = step.get("nativeParameters")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("selectiveEpitaxy nativeParameters must be a JSON object")

    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        # ``advanced`` lands here too: the parameter-object overload is not
        # implemented by this plugin, so leave the step to another path.
        return None

    duration = step.get("duration")
    if duration is None:
        raise ValueError("selectiveEpitaxy step requires duration")
    if "materialRates" not in raw:
        raise ValueError("selectiveEpitaxy step requires nativeParameters.materialRates")

    values: dict[str, Any] = {
        "duration": _number(duration, "duration"),
        "materialRates": material_rates(raw["materialRates"], "nativeParameters.materialRates"),
    }
    for name in ("rate111", "rate100"):
        if name in raw:
            values[name] = _number(raw[name], f"nativeParameters.{name}")
    if "nvFactors" in raw:
        values["nvFactors"] = nv_factors(raw["nvFactors"], "nativeParameters.nvFactors")

    advection = step.get("advection")
    if advection is not None:
        if not isinstance(advection, Mapping):
            raise ValueError("selectiveEpitaxy advection must be a JSON object or null")
        values["advection"] = dict(advection)

    return SelectiveEpitaxyParams(**values)
