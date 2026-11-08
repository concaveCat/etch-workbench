"""Canonical parameters for the native Faraday-cage ion-beam-etch plugin.

The nineteen scalar fields and the per-material plane-wafer-rate table mirror
``backend.specialized_models.CATALOG`` for the ``faradayCage`` row (``cageAngle``
plus the shared ``ibe_fields()``). They are repeated here instead of imported so
that importing the plugin never imports :mod:`backend` (the plugin must load
without the worker or the API layer). The bounds are the same ones
``backend.validation`` enforces on ``nativeParameters``:
``0 <= value <= 1e6`` for energies/rates/yields, ``0 <= angle <= 180`` for the
four acceptance angles and ``cageAngle``, and ``-180 <= tiltAngle <= 180``.

``thresholdEnergy < meanEnergy`` is a native invariant, not a catalog range:
ViennaPS compares the sampled energy with ``thresholdEnergy`` when it normalizes
the FaradayCage yield, and ``thresholdEnergy >= meanEnergy`` reverses the flux
sign (an etch then grows the surface, aborts the ray trace, or empties the top
level set). The model rejects it after field validation so every accepted
parameter object is safe to hand to the native constructor.

``materialPlaneWaferRateByMaterial`` maps material names to the native
``IBEParameters.materialPlaneWaferRate`` entries. Names are carried verbatim;
resolving them against the installed ``MaterialRegistry`` happens at execute
time, where an unknown name raises instead of silently using a default.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "COS4_FIELDS",
    "FaradayCageParams",
    "IBE_SCALAR_FIELDS",
    "MATERIAL_RATE_FIELD",
    "SCALAR_FIELDS",
]

#: ``IBEParameters`` scalars set by ``worker/specialized.py`` for faradayCage.
IBE_SCALAR_FIELDS: tuple[str, ...] = (
    "meanEnergy",
    "sigmaEnergy",
    "thresholdEnergy",
    "thetaRMin",
    "thetaRMax",
    "minAngle",
    "inflectAngle",
    "exponent",
    "n_l",
    "tiltAngle",
    "planeWaferRate",
    "redepositionRate",
    "redepositionThreshold",
)

#: Flat catalog keys copied into ``IBEParameters.cos4Yield.a1..a4``.
COS4_FIELDS: tuple[str, ...] = (
    "cos4YieldA1",
    "cos4YieldA2",
    "cos4YieldA3",
    "cos4YieldA4",
)

#: Every scalar parameter, in catalog order (``cageAngle`` first).
SCALAR_FIELDS: tuple[str, ...] = ("cageAngle", *IBE_SCALAR_FIELDS, *COS4_FIELDS)

#: Per-material native ``planeWaferRate`` table.
MATERIAL_RATE_FIELD = "materialPlaneWaferRateByMaterial"

_MAXIMUM = 1_000_000.0
_ANGLE_MAXIMUM = 180.0

_DEFAULTS: dict[str, float] = {
    "cageAngle": 0.0,
    "meanEnergy": 100.0,
    "sigmaEnergy": 10.0,
    "thresholdEnergy": 20.0,
    "thetaRMin": 70.0,
    "thetaRMax": 90.0,
    "minAngle": 85.0,
    "inflectAngle": 89.0,
    "exponent": 100.0,
    "n_l": 10.0,
    "tiltAngle": 0.0,
    "planeWaferRate": 0.1,
    "redepositionRate": 0.0,
    "redepositionThreshold": 0.1,
    "cos4YieldA1": 0.0,
    "cos4YieldA2": 0.0,
    "cos4YieldA3": 0.0,
    "cos4YieldA4": 0.0,
}


class FaradayCageParams(BaseModel):
    """Parameters of one native ``ps.FaradayCageEtching`` step.

    Every numeric field must be finite; the ranges are the catalog's own so a
    step accepted by ``backend.validation`` is accepted here and vice versa.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    duration: float = Field(gt=0)
    cageAngle: float = Field(default=_DEFAULTS["cageAngle"], ge=0.0, le=_ANGLE_MAXIMUM)
    meanEnergy: float = Field(default=_DEFAULTS["meanEnergy"], ge=0.0, le=_MAXIMUM)
    sigmaEnergy: float = Field(default=_DEFAULTS["sigmaEnergy"], ge=0.0, le=_MAXIMUM)
    thresholdEnergy: float = Field(default=_DEFAULTS["thresholdEnergy"], ge=0.0, le=_MAXIMUM)
    thetaRMin: float = Field(default=_DEFAULTS["thetaRMin"], ge=0.0, le=_ANGLE_MAXIMUM)
    thetaRMax: float = Field(default=_DEFAULTS["thetaRMax"], ge=0.0, le=_ANGLE_MAXIMUM)
    minAngle: float = Field(default=_DEFAULTS["minAngle"], ge=0.0, le=_ANGLE_MAXIMUM)
    inflectAngle: float = Field(default=_DEFAULTS["inflectAngle"], ge=0.0, le=_ANGLE_MAXIMUM)
    exponent: float = Field(default=_DEFAULTS["exponent"], ge=0.0, le=_MAXIMUM)
    n_l: float = Field(default=_DEFAULTS["n_l"], ge=0.0, le=_MAXIMUM)
    tiltAngle: float = Field(default=_DEFAULTS["tiltAngle"], ge=-_ANGLE_MAXIMUM, le=_ANGLE_MAXIMUM)
    planeWaferRate: float = Field(default=_DEFAULTS["planeWaferRate"], ge=0.0, le=_MAXIMUM)
    redepositionRate: float = Field(default=_DEFAULTS["redepositionRate"], ge=0.0, le=_MAXIMUM)
    redepositionThreshold: float = Field(default=_DEFAULTS["redepositionThreshold"], ge=0.0, le=_MAXIMUM)
    cos4YieldA1: float = Field(default=_DEFAULTS["cos4YieldA1"], ge=0.0, le=_MAXIMUM)
    cos4YieldA2: float = Field(default=_DEFAULTS["cos4YieldA2"], ge=0.0, le=_MAXIMUM)
    cos4YieldA3: float = Field(default=_DEFAULTS["cos4YieldA3"], ge=0.0, le=_MAXIMUM)
    cos4YieldA4: float = Field(default=_DEFAULTS["cos4YieldA4"], ge=0.0, le=_MAXIMUM)
    materialPlaneWaferRateByMaterial: dict[str, float] = Field(default_factory=dict)

    @field_validator(*SCALAR_FIELDS, "duration")
    @classmethod
    def _finite(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    @field_validator(MATERIAL_RATE_FIELD)
    @classmethod
    def _material_rates(cls, value: dict[str, float]) -> dict[str, float]:
        rates: dict[str, float] = {}
        for name, rate in value.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"{MATERIAL_RATE_FIELD} keys must be non-empty material names")
            if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                raise ValueError(f"{MATERIAL_RATE_FIELD}.{name} must be a finite number")
            number = float(rate)
            if not math.isfinite(number):
                raise ValueError(f"{MATERIAL_RATE_FIELD}.{name} must be finite")
            if number < 0.0 or number > _MAXIMUM:
                raise ValueError(f"{MATERIAL_RATE_FIELD}.{name} must be within [0, {_MAXIMUM:g}]")
            rates[name] = number
        return rates

    @model_validator(mode="after")
    def _energy_order(self) -> "FaradayCageParams":
        if self.thresholdEnergy >= self.meanEnergy:
            raise ValueError(
                "thresholdEnergy must be less than meanEnergy; ViennaPS compares the sampled energy "
                "with thresholdEnergy when it normalizes the FaradayCageEtching yield, and "
                "thresholdEnergy >= meanEnergy reverses the native flux sign"
            )
        return self

    def ibe_values(self) -> dict[str, float]:
        """Return the thirteen ``IBEParameters`` scalars keyed by native name."""
        return {name: float(getattr(self, name)) for name in IBE_SCALAR_FIELDS}

    def cos4_values(self) -> dict[str, float]:
        """Return the four cos4-yield coefficients keyed as ``a1``..``a4``."""
        return {f"a{index}": float(getattr(self, name)) for index, name in enumerate(COS4_FIELDS, start=1)}

    def scalar_values(self) -> dict[str, float]:
        """Return every scalar parameter keyed by its flat catalog name."""
        return {name: float(getattr(self, name)) for name in SCALAR_FIELDS}

    def material_rates(self) -> dict[str, float]:
        """Return the per-material plane-wafer rates in sorted name order."""
        return {name: float(value) for name, value in sorted(self.materialPlaneWaferRateByMaterial.items())}
