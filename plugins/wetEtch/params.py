"""Canonical parameters for the native crystalline wet-etch plugin.

The eight flat fields and their defaults/ranges mirror
``backend.specialized_models`` for the ``wetEtch`` catalog row. They are
repeated here instead of imported so that importing the plugin never imports
:mod:`backend` (the plugin must load without the worker or the API layer).

Wet-etch rates are positive etch magnitudes, so every rate is ``>= 0``. The
``materialRates`` table is authoritative: the native model receives exactly one
``(material handle, rate)`` row per entry, sorted by material name. The table
may be empty in the model (the catalog default), but
:func:`plugins.wetEtch.step.parse_step` does not claim a step without it.

The crystalline fields are inert unless ``useCrystallineRates`` is true, but
they are validated unconditionally: a project that carries a malformed
direction vector is rejected even when the non-crystalline overload would be
used.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["FLAT_FIELDS", "RATE_FIELDS", "WetEtchParams"]

#: Catalog field order of the ``wetEtch`` row.
FLAT_FIELDS: tuple[str, ...] = (
    "materialRates",
    "useCrystallineRates",
    "direction100",
    "direction010",
    "rate100",
    "rate110",
    "rate111",
    "rate311",
)

#: Scalar rate fields, all finite and in ``[0, 1e6]``.
RATE_FIELDS: tuple[str, ...] = ("rate100", "rate110", "rate111", "rate311")

_MAXIMUM = 1_000_000.0

_DEFAULT_DIRECTIONS: dict[str, list[float]] = {
    "direction100": [1.0, 0.0, 0.0],
    "direction010": [0.0, 1.0, 0.0],
}

_DEFAULTS: dict[str, float] = {
    "rate100": 0.1,
    "rate110": 0.1,
    "rate111": 0.1,
    "rate311": 0.1,
}


def _finite_number(value: Any, name: str) -> float:
    """Return ``value`` as a finite float or raise ``ValueError``.

    Booleans and strings are rejected on purpose: the project schema only
    carries JSON numbers here, and coercing a string would change what the
    native constructor receives.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite JSON number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return number


def _direction(value: Any, name: str) -> list[float]:
    """Return ``value`` as a finite, non-zero three-vector."""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a three-component list, got {value!r}")
    if len(value) != 3:
        raise ValueError(f"{name} must contain exactly three numbers, got {value!r}")
    numbers = [_finite_number(item, f"{name}[{index}]") for index, item in enumerate(value)]
    if all(number == 0.0 for number in numbers):
        raise ValueError(f"{name} must not be the zero vector")
    return numbers


def _material_rates(value: Any) -> dict[str, float]:
    """Return a validated, name-sorted material-rate table."""
    if not isinstance(value, Mapping):
        raise ValueError(f"materialRates must be a JSON object, got {value!r}")
    rates: dict[str, float] = {}
    for name, rate in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"materialRates keys must be non-empty material names, got {name!r}")
        number = _finite_number(rate, f"materialRates.{name}")
        if number < 0.0:
            raise ValueError(f"materialRates.{name} must be >= 0 for a wet etch, got {number!r}")
        if number > _MAXIMUM:
            raise ValueError(f"materialRates.{name} must be <= {_MAXIMUM:g}, got {number!r}")
        rates[name] = number
    return dict(sorted(rates.items()))


class WetEtchParams(BaseModel):
    """Parameters of one native ``ps.WetEtching`` step.

    ``materialRates`` maps native material names to non-negative etch rates.
    When ``useCrystallineRates`` is true the two direction vectors and the
    four orientation rates select the crystalline overload; otherwise only the
    material-rate overload is used and those fields stay inert.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    duration: float = Field(gt=0)
    materialRates: dict[str, float] = Field(default_factory=dict)
    useCrystallineRates: bool = False
    direction100: list[float] = Field(default_factory=lambda: list(_DEFAULT_DIRECTIONS["direction100"]))
    direction010: list[float] = Field(default_factory=lambda: list(_DEFAULT_DIRECTIONS["direction010"]))
    rate100: float = Field(default=_DEFAULTS["rate100"], ge=0.0, le=_MAXIMUM)
    rate110: float = Field(default=_DEFAULTS["rate110"], ge=0.0, le=_MAXIMUM)
    rate111: float = Field(default=_DEFAULTS["rate111"], ge=0.0, le=_MAXIMUM)
    rate311: float = Field(default=_DEFAULTS["rate311"], ge=0.0, le=_MAXIMUM)

    @field_validator("duration", *RATE_FIELDS, mode="before")
    @classmethod
    def _numbers(cls, value: Any, info: Any) -> float:
        return _finite_number(value, info.field_name)

    @field_validator("direction100", "direction010", mode="before")
    @classmethod
    def _directions(cls, value: Any, info: Any) -> list[float]:
        return _direction(value, info.field_name)

    @field_validator("materialRates", mode="before")
    @classmethod
    def _rates(cls, value: Any) -> dict[str, float]:
        return _material_rates(value)

    def sorted_material_rates(self) -> tuple[tuple[str, float], ...]:
        """Return ``(material name, rate)`` rows sorted by material name."""
        return tuple(sorted((name, float(rate)) for name, rate in self.materialRates.items()))

    def native_parameters(self) -> dict[str, Any]:
        """Return the validated values in a JSON-safe metadata shape."""
        return {
            "materialRates": {name: float(rate) for name, rate in self.sorted_material_rates()},
            "useCrystallineRates": bool(self.useCrystallineRates),
            "direction100": list(self.direction100),
            "direction010": list(self.direction010),
            "rate100": float(self.rate100),
            "rate110": float(self.rate110),
            "rate111": float(self.rate111),
            "rate311": float(self.rate311),
        }
