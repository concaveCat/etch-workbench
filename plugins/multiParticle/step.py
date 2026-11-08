"""Worker step adapter for the native multi-particle plasma plugin.

A raw worker step carries ``model="multiParticle"`` next to ``operation``,
``duration``, the optional ``depositMaterial``, the species list, and the
rate-coefficient table. :func:`parse_step` is the plugin's declared
:class:`~process_models.v2.contract.ProcessSpec` hook: it returns
:class:`~plugins.multiParticle.params.MultiParticleParams` for such a step and
``None`` for every other step. Malformed values raise ``ValueError``.

Keys outside the multi-particle allowlist return ``None``, including
``advanced`` and the parameter-object input shape: the caller keeps its own
path instead of silently running a different simulation. The same rule applies
to unknown species or coefficient keys, because a native option this plugin
does not model must not be dropped silently.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .params import MultiParticleParams

__all__ = ["parse_step"]

_ALLOWED_KEYS = frozenset(
    {
        "id",
        "name",
        "enabled",
        "model",
        "operation",
        "duration",
        "depositMaterial",
        "species",
        "rateCoefficients",
    }
)

_ION_KEYS = frozenset(
    {
        "kind",
        "label",
        "sourcePower",
        "thetaRMin",
        "thetaRMax",
        "minAngle",
        "B_sp",
        "meanEnergy",
        "sigmaEnergy",
        "thresholdEnergy",
        "inflectAngle",
        "n",
    }
)

_NEUTRAL_KEYS = frozenset(
    {
        "kind",
        "label",
        "defaultStickingProbability",
        "stickingProbabilityByMaterial",
    }
)

_COEFFICIENT_KEYS = frozenset({"defaultBySpecies", "byMaterial"})

_ION_NUMBERS = (
    "sourcePower",
    "thetaRMin",
    "thetaRMax",
    "minAngle",
    "B_sp",
    "meanEnergy",
    "sigmaEnergy",
    "thresholdEnergy",
    "inflectAngle",
)


def _number(value: Any, path: str) -> float:
    """Return ``value`` as a finite float or raise ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite JSON number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{path} must be finite, got {value!r}")
    return number


def _integer(value: Any, path: str) -> int:
    """Return ``value`` as a JSON integer or raise ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} must be a JSON integer, got {value!r}")
    return int(value)


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a JSON object")
    return value


def _parse_species(raw: Any) -> list[dict[str, Any]] | None:
    """Return species dictionaries, or ``None`` for an unmodelled shape."""
    if not isinstance(raw, list):
        raise ValueError("multiParticle species must be a JSON array")
    species: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        path = f"species[{index}]"
        values = _mapping(item, path)
        kind = values.get("kind")
        if kind == "ion":
            if set(values) - _ION_KEYS:
                return None
            parsed: dict[str, Any] = {"kind": "ion", "label": values.get("label")}
            for name in _ION_NUMBERS:
                if name in values:
                    parsed[name] = _number(values[name], f"{path}.{name}")
            if "n" in values:
                parsed["n"] = _integer(values["n"], f"{path}.n")
            species.append(parsed)
        elif kind == "neutral":
            if set(values) - _NEUTRAL_KEYS:
                return None
            parsed = {"kind": "neutral", "label": values.get("label")}
            if "defaultStickingProbability" in values:
                parsed["defaultStickingProbability"] = _number(
                    values["defaultStickingProbability"], f"{path}.defaultStickingProbability"
                )
            if "stickingProbabilityByMaterial" in values:
                raw_sticking = _mapping(
                    values["stickingProbabilityByMaterial"], f"{path}.stickingProbabilityByMaterial"
                )
                sticking: dict[str, float] = {}
                for material, probability in raw_sticking.items():
                    if not isinstance(material, str) or not material.strip():
                        raise ValueError(f"{path}.stickingProbabilityByMaterial keys must be non-empty material names")
                    sticking[material] = _number(probability, f"{path}.stickingProbabilityByMaterial.{material}")
                parsed["stickingProbabilityByMaterial"] = sticking
            species.append(parsed)
        else:
            raise ValueError(f"{path}.kind must be 'ion' or 'neutral', got {kind!r}")
    return species


def _parse_coefficients(raw: Any) -> dict[str, Any] | None:
    """Return coefficient dictionaries, or ``None`` for an unmodelled shape."""
    values = _mapping(raw, "multiParticle rateCoefficients")
    if set(values) - _COEFFICIENT_KEYS:
        return None
    raw_defaults = _mapping(values.get("defaultBySpecies"), "rateCoefficients.defaultBySpecies")
    defaults: dict[str, float] = {}
    for label, coefficient in raw_defaults.items():
        if not isinstance(label, str) or not label.strip():
            raise ValueError("rateCoefficients.defaultBySpecies keys must be non-empty species labels")
        defaults[label] = _number(coefficient, f"rateCoefficients.defaultBySpecies.{label}")

    overrides: dict[str, dict[str, float]] = {}
    raw_overrides = values.get("byMaterial", {})
    for material, row in _mapping(raw_overrides, "rateCoefficients.byMaterial").items():
        if not isinstance(material, str) or not material.strip():
            raise ValueError("rateCoefficients.byMaterial keys must be non-empty material names")
        parsed_row: dict[str, float] = {}
        for label, coefficient in _mapping(row, f"rateCoefficients.byMaterial.{material}").items():
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"rateCoefficients.byMaterial.{material} keys must be non-empty species labels")
            parsed_row[label] = _number(coefficient, f"rateCoefficients.byMaterial.{material}.{label}")
        overrides[material] = parsed_row
    return {"defaultBySpecies": defaults, "byMaterial": overrides}


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> MultiParticleParams | None:
    """Return validated params when ``step`` is a multi-particle step.

    ``project`` is accepted for the common ``(project, step)`` hook signature;
    nothing in the project switches dispatch today. A step that claims to be
    ``multiParticle`` but lacks ``operation``, ``duration``, ``species``, or
    ``rateCoefficients`` raises ``ValueError`` instead of being skipped.
    """
    del project
    if step.get("model") != "multiParticle":
        return None

    if set(step) - _ALLOWED_KEYS:
        # ``advanced``, ``nativeParameters``, and any unmodelled field are not
        # part of this overload.
        return None

    operation = step.get("operation")
    if operation is None:
        raise ValueError("multiParticle step requires operation")
    duration = step.get("duration")
    if duration is None:
        raise ValueError("multiParticle step requires duration")
    raw_species = step.get("species")
    if raw_species is None:
        raise ValueError("multiParticle step requires species")
    raw_coefficients = step.get("rateCoefficients")
    if raw_coefficients is None:
        raise ValueError("multiParticle step requires rateCoefficients")

    species = _parse_species(raw_species)
    if species is None:
        return None
    coefficients = _parse_coefficients(raw_coefficients)
    if coefficients is None:
        return None

    values: dict[str, Any] = {
        "operation": operation,
        "duration": _number(duration, "duration"),
        "species": species,
        "rateCoefficients": coefficients,
    }
    if "depositMaterial" in step:
        values["depositMaterial"] = step["depositMaterial"]
    return MultiParticleParams(**values)
