"""Worker step adapter for the native ion-beam etch (IBE) plugin.

A raw worker step for this process carries ``model="specialized"``,
``nativeModel="ionBeam"``, a flat ``nativeParameters`` object, and ``duration``.
:func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.ionBeam.params.IonBeamParams` for such a step and ``None`` for
every other step. Malformed values raise ``ValueError``.

``nativeParameters.advanced`` and any key outside the flat allowlist return
``None``: the flat/parameter-object path implemented here is the only one this
plugin claims, so the caller keeps the old worker path instead of silently
running a different simulation. ``materialPlaneWaferRateByMaterial`` is the one
non-scalar key that is accepted, because it is applied to the native
``IBEParameters.materialPlaneWaferRate`` map.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import MATERIAL_RATE_FIELD, SCALAR_FIELDS, IonBeamParams

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


def _material_rates(value: Any, path: str) -> dict[str, float]:
    """Return a validated material-name-to-rate table or raise ``ValueError``."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a JSON object keyed by material name")
    rates: dict[str, float] = {}
    for name, rate in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path} keys must be non-empty material names")
        number = _number(rate, f"{path}.{name}")
        if number < 0.0:
            raise ValueError(f"{path}.{name} must be non-negative, got {rate!r}")
        rates[name] = number
    return rates


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> IonBeamParams | None:
    """Return validated params when ``step`` is a flat ion-beam etch step.

    ``project`` is accepted for the common ``(project, step)`` hook signature;
    nothing in the project switches dispatch today. A step that claims to be
    ``specialized``/``ionBeam`` but lacks ``duration`` or carries an invalid
    flat value raises ``ValueError`` instead of being skipped. The energy order
    rule is enforced by :class:`~plugins.ionBeam.params.IonBeamParams`, so a
    ``thresholdEnergy >= meanEnergy`` step is rejected before any native object
    is constructed.
    """
    del project
    if step.get("model") != "specialized":
        return None
    if step.get("nativeModel") != "ionBeam":
        return None

    raw = step.get("nativeParameters")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("ionBeam nativeParameters must be a JSON object")

    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        # ``advanced`` lands here too: the parameter-object overload beyond the
        # flat fields is not implemented by this plugin, so leave the step to
        # the worker path.
        return None

    duration = step.get("duration")
    if duration is None:
        raise ValueError("ionBeam step requires duration")

    values: dict[str, Any] = {"duration": _number(duration, "duration")}
    for name in SCALAR_FIELDS:
        if name in raw:
            values[name] = _number(raw[name], f"nativeParameters.{name}")
    if MATERIAL_RATE_FIELD in raw:
        values[MATERIAL_RATE_FIELD] = _material_rates(
            raw[MATERIAL_RATE_FIELD], f"nativeParameters.{MATERIAL_RATE_FIELD}"
        )

    return IonBeamParams(**values)
