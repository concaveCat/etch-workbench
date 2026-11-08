"""Worker step adapter for the native silicon thermal-oxidation plugin.

A raw worker step for this process carries ``model="specialized"``,
``nativeModel="oxidation"``, ``operation="native"``, and a flat
``nativeParameters`` object. :func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.oxidation.params.OxidationParams` for such a step and ``None``
for every other step. Malformed values raise ``ValueError``.

Two paths are deliberately left to the old worker implementation:

``maskMaterial``
    A non-empty mask material relies on the native LOCOS mask mechanics, which
    has no verified output on this host (the controlled probe collapsed the
    oxide level set). Such a step is declined instead of running a simulation
    nobody can validate.

``advancedOxidation``
    The solver-control object is not part of this slice. A step that carries it
    is declined, so it keeps the worker's parameter-object path.

Any key outside the flat allowlist is declined for the same reason: this
plugin implements exactly one native construction sequence.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import NUMERIC_FIELDS, OxidationParams

__all__ = ["parse_step"]

_ALLOWED_KEYS = frozenset(OxidationParams.model_fields)
_REQUIRED_KEYS = frozenset({"timeHours"})


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


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> OxidationParams | None:
    """Return validated params when ``step`` is a flat oxidation step.

    ``project`` is accepted for the common ``(project, step)`` hook signature;
    nothing in the project switches dispatch today. A step that claims to be
    ``specialized``/``oxidation`` but lacks ``timeHours`` or carries an invalid
    flat value raises ``ValueError`` instead of being skipped.
    """
    del project
    if step.get("model") != "specialized":
        return None
    if step.get("nativeModel") != "oxidation":
        return None
    if step.get("operation") != "native":
        return None

    # Deferred: the advanced solver-control path stays in the worker.
    if step.get("advancedOxidation") is not None:
        return None

    raw = step.get("nativeParameters")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("oxidation nativeParameters must be a JSON object")

    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        # An ``advanced``-style key lands here too: only the flat path is
        # implemented, so leave the step to the worker path.
        return None

    mask_material = raw.get("maskMaterial", "")
    if not isinstance(mask_material, str):
        raise ValueError("nativeParameters.maskMaterial must be a material name string")
    if mask_material:
        # Deferred: mask mechanics is unverified on this host.
        return None

    missing = sorted(_REQUIRED_KEYS - set(raw))
    if missing:
        raise ValueError(f"oxidation step requires nativeParameters.{missing[0]}")

    values: dict[str, Any] = {}
    for name in NUMERIC_FIELDS:
        if name in raw:
            values[name] = _number(raw[name], f"nativeParameters.{name}")
    for name in _ALLOWED_KEYS - set(NUMERIC_FIELDS):
        if name == "maskMaterial":
            continue
        if name in raw:
            values[name] = raw[name]

    return OxidationParams(**values)
