"""Worker step adapter for the native single-particle plugin.

A raw worker step carries ``model="singleParticle"`` next to ``operation``,
``rateMagnitude``, ``duration``, the particle coefficients, and the optional
rate table. :func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.singleParticle.params.SingleParticleParams` for such a step and
``None`` for every other step. Malformed values raise ``ValueError``.

Keys outside the single-particle allowlist return ``None``. That covers
parameter-object/advanced input, ``calculateVisibility`` (the directional
model's visibility flag, not applicable here), and the isotropic dispatch
fields: the caller keeps its own path instead of silently running a different
simulation. ``selectivityReference`` is accepted and ignored because it is a
UI-only ratio reference that never changes the native simulation.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import SingleParticleParams

__all__ = ["parse_step"]

_ALLOWED_KEYS = frozenset(
    {
        "id",
        "name",
        "enabled",
        "model",
        "operation",
        "rateMagnitude",
        "duration",
        "depositMaterial",
        "stickingProbability",
        "sourceExponent",
        "rateMode",
        "materialRates",
        "selectivityReference",
    }
)


def _number(value: Any, path: str) -> float:
    """Return ``value`` as a finite float or raise ``ValueError``.

    Booleans and strings are rejected on purpose: project JSON only carries
    JSON numbers here, and coercing a string would change what the native
    constructor receives.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite JSON number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{path} must be finite, got {value!r}")
    return number


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> SingleParticleParams | None:
    """Return validated params when ``step`` is a single-particle step.

    ``project`` is accepted for the common ``(project, step)`` hook signature;
    nothing in the project switches dispatch today. A step that claims to be a
    single-particle step but lacks ``duration``, ``rateMagnitude``,
    ``stickingProbability``, or ``sourceExponent`` raises ``ValueError`` instead
    of being skipped.
    """
    del project
    if step.get("model") != "singleParticle":
        return None

    if set(step) - _ALLOWED_KEYS:
        # ``nativeParameters``/``advanced``/``calculateVisibility`` and the
        # isotropic dispatch fields are not part of this overload.
        return None

    duration = step.get("duration")
    if duration is None:
        raise ValueError("singleParticle step requires duration")
    rate_magnitude = step.get("rateMagnitude")
    if rate_magnitude is None:
        raise ValueError("singleParticle step requires rateMagnitude")
    sticking = step.get("stickingProbability")
    if sticking is None:
        raise ValueError("singleParticle step requires stickingProbability")
    source_exponent = step.get("sourceExponent")
    if source_exponent is None:
        raise ValueError("singleParticle step requires sourceExponent")

    values: dict[str, Any] = {
        "operation": step.get("operation"),
        "rateMagnitude": _number(rate_magnitude, "rateMagnitude"),
        "duration": _number(duration, "duration"),
        "stickingProbability": _number(sticking, "stickingProbability"),
        "sourceExponent": _number(source_exponent, "sourceExponent"),
    }
    if "depositMaterial" in step:
        values["depositMaterial"] = step["depositMaterial"]
    if "rateMode" in step:
        values["rateMode"] = step["rateMode"]
    if "materialRates" in step:
        raw_rates = step["materialRates"]
        if raw_rates is None:
            values["materialRates"] = None
        else:
            if not isinstance(raw_rates, Mapping):
                raise ValueError("singleParticle materialRates must be a JSON object")
            rates: dict[str, float] = {}
            for name, rate in raw_rates.items():
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("materialRates keys must be non-empty material names")
                rates[name] = _number(rate, f"materialRates.{name}")
            values["materialRates"] = rates

    return SingleParticleParams(**values)
