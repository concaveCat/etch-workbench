"""Worker step adapter for the native oxide-regrowth plugin.

A raw worker step for this process carries ``model="specialized"``,
``nativeModel="oxideRegrowth"``, ``operation="native"``, a flat
``nativeParameters`` object, and a top-level ``duration``.
:func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.oxideRegrowth.params.OxideRegrowthParams` for such a step and
``None`` for every other step. Malformed values raise ``ValueError``.

Any key outside the flat allowlist returns ``None``: the flat overload is the
only path this plugin implements, so an advanced parameter object stays on the
old worker path instead of silently running a different simulation. A step that
carries an explicit ``advanced``/``advancedOxideRegrowth`` object is declined
for the same reason.

Unlike ``Oxidation``, the native ``ps.OxideRegrowth`` model has no internal
time control, so ``duration`` is required and must be positive.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import FLAT_FIELDS, OxideRegrowthParams

__all__ = ["parse_step"]

_ALLOWED_KEYS = frozenset(FLAT_FIELDS)
_ADVANCED_KEYS = frozenset({"advanced", "advancedOxideRegrowth"})


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


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> OxideRegrowthParams | None:
    """Return validated params when ``step`` is a flat oxide-regrowth step.

    ``project`` is accepted for the common ``(project, step)`` hook signature;
    nothing in the project switches dispatch today. A step that claims to be
    ``specialized``/``oxideRegrowth``/``native`` but lacks ``duration`` or
    carries an invalid flat value raises ``ValueError`` instead of being
    skipped.
    """
    del project
    if step.get("model") != "specialized":
        return None
    if step.get("nativeModel") != "oxideRegrowth":
        return None
    if step.get("operation") != "native":
        return None

    # Deferred: the advanced parameter-object path stays in the worker.
    if any(step.get(key) is not None for key in _ADVANCED_KEYS):
        return None

    raw = step.get("nativeParameters")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("oxideRegrowth nativeParameters must be a JSON object")

    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        # An ``advanced``-style key lands here too: only the flat path is
        # implemented, so leave the step to the worker path.
        return None

    duration = step.get("duration")
    if duration is None:
        raise ValueError("oxideRegrowth step requires duration")

    values: dict[str, Any] = {"duration": _number(duration, "duration")}
    for name in FLAT_FIELDS:
        if name in raw:
            values[name] = _number(raw[name], f"nativeParameters.{name}")

    return OxideRegrowthParams(**values)
