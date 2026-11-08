"""Worker step adapter for the native fluorocarbon plugin.

A raw worker step for this process carries ``model="specialized"``,
``nativeModel="fluorocarbon"``, a flat ``nativeParameters`` object of the
fourteen scalars plus ``materialParametersByMaterial``, and ``duration``.
:func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.fluorocarbon.params.FluorocarbonParams` for such a step and
``None`` for every other step.

``nativeParameters.advanced`` and any top-level key outside the allowlist
return ``None``: the object overload implemented here is the worker's own
``FluorocarbonParameters`` construction, and a step carrying a different
overload keeps the old worker path instead of silently running a different
simulation. Inside the material table the rules are strict instead: a row must
be a JSON object with exactly the ten native fields, and material names must be
catalog materials or ``Polymer``. Those raise ``ValueError`` because the table
is part of this process's contract and silently dropping an unknown row would
change which native parameters reach the model.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import MATERIAL_FIELDS, SCALAR_FIELDS, FluorocarbonParams

__all__ = ["parse_step"]

_MATERIALS_KEY = "materialParametersByMaterial"
_ALLOWED_KEYS = frozenset(SCALAR_FIELDS) | {_MATERIALS_KEY}
_ROW_KEYS = frozenset(MATERIAL_FIELDS)


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


def _material_table(raw: Any) -> dict[str, dict[str, float]]:
    """Validate the material-keyed table down to plain JSON numbers.

    Unknown material names are rejected by
    :class:`~plugins.fluorocarbon.params.FluorocarbonParams`; this function
    rejects malformed rows before they reach the parameter model so the error
    names the offending material and field.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("fluorocarbon nativeParameters.materialParametersByMaterial must be a JSON object")
    table: dict[str, dict[str, float]] = {}
    for name, row in raw.items():
        if not isinstance(name, str):
            raise ValueError(f"fluorocarbon material name must be a string, got {name!r}")
        path = f"nativeParameters.materialParametersByMaterial.{name}"
        if not isinstance(row, Mapping):
            raise ValueError(f"{path} must be a JSON object")
        unknown = sorted(set(row) - _ROW_KEYS)
        if unknown:
            raise ValueError(f"{path} contains unknown fields {unknown}")
        missing = sorted(_ROW_KEYS - set(row))
        if missing:
            raise ValueError(f"{path} is missing fields {missing}; every row needs all ten fields")
        table[name] = {key: _number(row[key], f"{path}.{key}") for key in MATERIAL_FIELDS}
    return table


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> FluorocarbonParams | None:
    """Return validated params when ``step`` is a fluorocarbon etch step.

    ``project`` is accepted for the common ``(project, step)`` hook signature;
    nothing in the project switches dispatch today. A step that claims to be
    ``specialized``/``fluorocarbon`` but lacks ``duration`` or carries an
    invalid scalar or material row raises ``ValueError`` instead of being
    skipped.
    """
    del project
    if step.get("model") != "specialized":
        return None
    if step.get("nativeModel") != "fluorocarbon":
        return None

    raw = step.get("nativeParameters")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("fluorocarbon nativeParameters must be a JSON object")

    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        # ``advanced`` and ``coverageTolerance`` land here too: neither the
        # parameter-object overload nor a coverage pass is part of this
        # process, so leave the step to the worker path.
        return None

    duration = step.get("duration")
    if duration is None:
        raise ValueError("fluorocarbon step requires duration")

    values: dict[str, Any] = {"duration": _number(duration, "duration")}
    for name in SCALAR_FIELDS:
        if name in raw:
            values[name] = _number(raw[name], f"nativeParameters.{name}")
    if _MATERIALS_KEY in raw:
        values[_MATERIALS_KEY] = _material_table(raw[_MATERIALS_KEY])

    return FluorocarbonParams(**values)
