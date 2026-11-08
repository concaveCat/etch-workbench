"""Worker step adapter for the native GeometricTrenchDeposition plugin.

A raw worker step for this process carries ``model="specialized"``,
``nativeModel="geometricTrenchDeposition"``, ``operation="deposit"``, a
``depositMaterial``, a flat seven-field ``nativeParameters`` object, and
``duration``. :func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.geometricTrenchDeposition.params.GeometricTrenchDepositionParams`
for such a step and ``None`` for every other step. Malformed values raise
``ValueError``.

``nativeParameters.advanced`` and any key outside the flat allowlist return
``None``: the flat constructor is the only path this plugin implements, so the
caller keeps the old worker path for advanced parameter objects instead of
silently running a different simulation.

GDS geometry returns ``None``: the native radius formula is evaluated from the
absolute z coordinate, and a GDS project description does not bound that span,
so the worker (and this plugin) cannot prove the request is safe. A step with
``depositMaterial`` missing or empty is *this* process with an invalid field,
so it raises ``ValueError`` instead of being skipped.

The reach guard runs here as well as in the variant. A direct
``parse_step``/``registry.resolve_step`` call must reject a request that could
SIGSEGV the native kernel before any native object is constructed.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import FLAT_FIELDS, GeometricTrenchDepositionParams

__all__ = ["parse_step"]

_ALLOWED_KEYS = frozenset(FLAT_FIELDS)
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


def _unsupported_geometry(project: Mapping[str, Any]) -> str | None:
    """Return a reason when the project geometry cannot use this process.

    The native kernel only understands the parametric plane/trench/hole/fin/
    stack geometry builders the worker already creates. A GDS level set has no
    validated vertical span, so this plugin declines the step and the worker
    keeps its own rejection path.
    """
    geometry = project.get("geometry")
    if isinstance(geometry, Mapping) and geometry.get("kind") == "gds":
        return "GDS geometry"
    return None


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> GeometricTrenchDepositionParams | None:
    """Return validated params when ``step`` is a flat geometric-trench step.

    ``None`` means the step is not this process (other model/operation, GDS
    geometry, unsupported dimension, or a parameter key outside the flat
    allowlist). A step that claims to be
    ``specialized``/``geometricTrenchDeposition``/``deposit`` but lacks
    ``duration``/``depositMaterial``, carries an invalid flat value, or
    violates the reach guard raises ``ValueError``.
    """
    if step.get("model") != "specialized":
        return None
    if step.get("nativeModel") != "geometricTrenchDeposition":
        return None
    if step.get("operation") != "deposit":
        return None
    if project.get("dimension") not in (None, *_SUPPORTED_DIMENSIONS):
        return None
    if _unsupported_geometry(project) is not None:
        return None

    raw = step.get("nativeParameters")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("geometricTrenchDeposition nativeParameters must be a JSON object")

    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        # ``advanced`` lands here too: the parameter-object overload is not
        # implemented by this plugin, so leave the step to the worker path.
        return None

    deposit_material = step.get("depositMaterial")
    if not isinstance(deposit_material, str) or not deposit_material.strip():
        raise ValueError("geometricTrenchDeposition step requires a non-empty depositMaterial")

    duration = step.get("duration")
    if duration is None:
        raise ValueError("geometricTrenchDeposition step requires duration")

    values: dict[str, Any] = {
        "duration": _number(duration, "duration"),
        "depositMaterial": deposit_material,
    }
    for name in FLAT_FIELDS:
        if name in raw:
            values[name] = _number(raw[name], f"nativeParameters.{name}")

    params = GeometricTrenchDepositionParams(**values)
    if violation := params.reach_guard_violation():
        raise ValueError(f"geometricTrenchDeposition rejected before the native call: {violation}")
    return params
