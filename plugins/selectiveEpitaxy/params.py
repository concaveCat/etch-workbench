"""Canonical parameters for the native selective-epitaxy plugin.

The defaults/ranges mirror ``backend.specialized_models`` for the
``selectiveEpitaxy`` catalog row. They are repeated here instead of imported so
that importing the plugin never imports :mod:`backend` (the plugin must load
without the worker or the API layer).

``materialRates`` maps native material names to non-negative epitaxy rates and
must be non-empty: the native model has no useful default table, and the
catalog validation requires every material present at the step to be listed.
Rows are handed to the native constructor sorted by material name, exactly like
``worker/specialized.py``.

``nvFactors`` is the optional three-vector of the separate native constructor
overload. Omitting it keeps the ``materialRates`` overload (and therefore the
existing request hash); an explicit vector switches to
``SelectiveEpitaxy(nvFactors, rate111, rate100)`` and applies every rate through
``setMaterialRate`` afterwards. Only the shape (three finite numbers) is
enforced here; the installed model maps the factors itself.

There is no ``coverageTolerance`` and no ``advanced`` parameter object for this
model, so the plugin must not invent one.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from process_models.v2.options import AdvectionOverride

__all__ = [
    "FLAT_FIELDS",
    "OPTIONAL_FIELDS",
    "SelectiveEpitaxyParams",
    "material_rates",
    "nv_factors",
]

#: Native scalar constructor arguments of ``ps.SelectiveEpitaxy``.
FLAT_FIELDS: tuple[str, ...] = ("materialRates", "rate111", "rate100")

#: Optional keys that do not belong to the flat scalar argument list.
OPTIONAL_FIELDS: tuple[str, ...] = ("nvFactors",)

_MAXIMUM = 1_000_000.0

_DEFAULTS: dict[str, float] = {
    "rate111": 0.5,
    "rate100": 1.0,
}


def _finite_number(value: Any, path: str) -> float:
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


def material_rates(value: Any, path: str = "materialRates") -> dict[str, float]:
    """Return a validated, name-sorted material-rate table.

    The native model receives one ``(native material enum, rate)`` row per
    entry, so the table must not be empty and every rate must be a finite,
    non-negative number.
    """
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a JSON object, got {value!r}")
    rates: dict[str, float] = {}
    for name, rate in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path} keys must be non-empty material names, got {name!r}")
        number = _finite_number(rate, f"{path}.{name}")
        if number < 0.0:
            raise ValueError(f"{path}.{name} must be >= 0 for an epitaxy rate, got {number!r}")
        if number > _MAXIMUM:
            raise ValueError(f"{path}.{name} must be <= {_MAXIMUM:g}, got {number!r}")
        rates[name] = number
    if not rates:
        raise ValueError(f"{path} must contain at least one material rate")
    return dict(sorted(rates.items()))


def nv_factors(value: Any, path: str = "nvFactors") -> list[float]:
    """Return ``value`` as a three-component list of finite floats."""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{path} must be a three-component list, got {value!r}")
    if len(value) != 3:
        raise ValueError(f"{path} must contain exactly three numbers, got {value!r}")
    return [_finite_number(item, f"{path}[{index}]") for index, item in enumerate(value)]


class SelectiveEpitaxyParams(BaseModel):
    """Parameters of one native ``ps.SelectiveEpitaxy`` step.

    Every numeric field must be finite; the ranges are the catalog's own so a
    step accepted by the project validator is accepted here and vice versa.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    duration: float = Field(gt=0)
    materialRates: dict[str, float]
    rate111: float = Field(default=_DEFAULTS["rate111"], ge=0.0, le=_MAXIMUM)
    rate100: float = Field(default=_DEFAULTS["rate100"], ge=0.0, le=_MAXIMUM)
    nvFactors: list[float] | None = None
    advection: AdvectionOverride | None = None

    @field_validator("duration", "rate111", "rate100", mode="before")
    @classmethod
    def _numbers(cls, value: Any, info: Any) -> float:
        return _finite_number(value, info.field_name)

    @field_validator("materialRates", mode="before")
    @classmethod
    def _rates(cls, value: Any, info: Any) -> dict[str, float]:
        return material_rates(value, info.field_name)

    @field_validator("nvFactors", mode="before")
    @classmethod
    def _factors(cls, value: Any, info: Any) -> list[float] | None:
        if value is None:
            return None
        return nv_factors(value, info.field_name)

    def sorted_material_rates(self) -> tuple[tuple[str, float], ...]:
        """Return ``(material name, rate)`` rows sorted by material name."""
        return tuple(sorted((name, float(rate)) for name, rate in self.materialRates.items()))

    def native_parameters(self) -> dict[str, Any]:
        """Return the validated values in a JSON-safe metadata shape."""
        return {
            "materialRates": {name: float(rate) for name, rate in self.sorted_material_rates()},
            "rate111": float(self.rate111),
            "rate100": float(self.rate100),
            "nvFactors": None if self.nvFactors is None else [float(value) for value in self.nvFactors],
        }
