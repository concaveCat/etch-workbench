"""Canonical parameters for the native ion-beam etch (IBE) plugin.

The seventeen scalar fields and their defaults/ranges mirror
``backend.specialized_models.ibe_fields()`` for the ``ionBeam`` catalog row.
They are repeated here instead of imported so that importing the plugin never
imports :mod:`backend` (the plugin must load without the worker or the API
layer). The bounds are the same ones ``backend.validation`` enforces on
``nativeParameters``: angles are in ``[0, 180]`` except ``tiltAngle``, which is
in ``[-180, 180]``; every other scalar is in ``[0, 1e6]``.

``materialPlaneWaferRateByMaterial`` is an optional per-material override of
the native ``IBEParameters.materialPlaneWaferRate`` map. The catalog default is
an empty table and every referenced material defaults to ``0.0``; an empty
table therefore keeps the native ``planeWaferRate`` default for every material,
exactly like ``worker/specialized.py``. Masks are forced to ``0.0`` by the
native ``IonBeamEtching`` constructor, so listing them is redundant but
harmless.

ViennaPS normalizes the IBE yield with ``thresholdEnergy``. At
``thresholdEnergy >= meanEnergy`` that native comparison reverses the flux
sign, so an etch step grows the surface (or aborts the ray trace). The energy
order is rejected here, before any native object exists.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "MATERIAL_RATE_DEFAULT",
    "MATERIAL_RATE_FIELD",
    "SCALAR_FIELDS",
    "IonBeamParams",
]

#: Native ``IBEParameters`` scalar attributes set by the worker, in catalog order.
SCALAR_FIELDS: tuple[str, ...] = (
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
    "cos4YieldA1",
    "cos4YieldA2",
    "cos4YieldA3",
    "cos4YieldA4",
)

#: Per-material plane-wafer-rate table (native ``MaterialValueMap``).
MATERIAL_RATE_FIELD = "materialPlaneWaferRateByMaterial"

#: Catalog default of one explicit material rate.
MATERIAL_RATE_DEFAULT = 0.0

_MAXIMUM = 1_000_000.0
_ANGLE_MAXIMUM = 180.0

_DEFAULTS: dict[str, float] = {
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

_MINIMUMS: dict[str, float] = {name: 0.0 for name in SCALAR_FIELDS}
_MAXIMUMS: dict[str, float] = {name: _MAXIMUM for name in SCALAR_FIELDS}
for _angle in ("thetaRMin", "thetaRMax", "minAngle", "inflectAngle"):
    _MAXIMUMS[_angle] = _ANGLE_MAXIMUM
_MINIMUMS["tiltAngle"] = -_ANGLE_MAXIMUM
_MAXIMUMS["tiltAngle"] = _ANGLE_MAXIMUM


class IonBeamParams(BaseModel):
    """Parameters of one native ``ps.IonBeamEtching`` step.

    Every numeric field must be finite and inside the catalog range, so a step
    accepted by ``backend.validation`` is accepted here and vice versa.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    duration: float = Field(gt=0)
    meanEnergy: float = Field(default=_DEFAULTS["meanEnergy"], ge=_MINIMUMS["meanEnergy"], le=_MAXIMUMS["meanEnergy"])
    sigmaEnergy: float = Field(
        default=_DEFAULTS["sigmaEnergy"], ge=_MINIMUMS["sigmaEnergy"], le=_MAXIMUMS["sigmaEnergy"]
    )
    thresholdEnergy: float = Field(
        default=_DEFAULTS["thresholdEnergy"],
        ge=_MINIMUMS["thresholdEnergy"],
        le=_MAXIMUMS["thresholdEnergy"],
    )
    thetaRMin: float = Field(default=_DEFAULTS["thetaRMin"], ge=_MINIMUMS["thetaRMin"], le=_MAXIMUMS["thetaRMin"])
    thetaRMax: float = Field(default=_DEFAULTS["thetaRMax"], ge=_MINIMUMS["thetaRMax"], le=_MAXIMUMS["thetaRMax"])
    minAngle: float = Field(default=_DEFAULTS["minAngle"], ge=_MINIMUMS["minAngle"], le=_MAXIMUMS["minAngle"])
    inflectAngle: float = Field(
        default=_DEFAULTS["inflectAngle"], ge=_MINIMUMS["inflectAngle"], le=_MAXIMUMS["inflectAngle"]
    )
    exponent: float = Field(default=_DEFAULTS["exponent"], ge=_MINIMUMS["exponent"], le=_MAXIMUMS["exponent"])
    n_l: float = Field(default=_DEFAULTS["n_l"], ge=_MINIMUMS["n_l"], le=_MAXIMUMS["n_l"])
    tiltAngle: float = Field(default=_DEFAULTS["tiltAngle"], ge=_MINIMUMS["tiltAngle"], le=_MAXIMUMS["tiltAngle"])
    planeWaferRate: float = Field(
        default=_DEFAULTS["planeWaferRate"],
        ge=_MINIMUMS["planeWaferRate"],
        le=_MAXIMUMS["planeWaferRate"],
    )
    redepositionRate: float = Field(
        default=_DEFAULTS["redepositionRate"],
        ge=_MINIMUMS["redepositionRate"],
        le=_MAXIMUMS["redepositionRate"],
    )
    redepositionThreshold: float = Field(
        default=_DEFAULTS["redepositionThreshold"],
        ge=_MINIMUMS["redepositionThreshold"],
        le=_MAXIMUMS["redepositionThreshold"],
    )
    cos4YieldA1: float = Field(
        default=_DEFAULTS["cos4YieldA1"], ge=_MINIMUMS["cos4YieldA1"], le=_MAXIMUMS["cos4YieldA1"]
    )
    cos4YieldA2: float = Field(
        default=_DEFAULTS["cos4YieldA2"], ge=_MINIMUMS["cos4YieldA2"], le=_MAXIMUMS["cos4YieldA2"]
    )
    cos4YieldA3: float = Field(
        default=_DEFAULTS["cos4YieldA3"], ge=_MINIMUMS["cos4YieldA3"], le=_MAXIMUMS["cos4YieldA3"]
    )
    cos4YieldA4: float = Field(
        default=_DEFAULTS["cos4YieldA4"], ge=_MINIMUMS["cos4YieldA4"], le=_MAXIMUMS["cos4YieldA4"]
    )
    materialPlaneWaferRateByMaterial: dict[str, float] = Field(default_factory=dict)

    @field_validator(*SCALAR_FIELDS, "duration")
    @classmethod
    def _finite(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    @field_validator(MATERIAL_RATE_FIELD)
    @classmethod
    def _finite_material_rates(cls, value: dict[str, float]) -> dict[str, float]:
        rates: dict[str, float] = {}
        for name, rate in value.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"{MATERIAL_RATE_FIELD} keys must be non-empty material names")
            number = float(rate)
            if not math.isfinite(number):
                raise ValueError(f"{MATERIAL_RATE_FIELD}.{name} must be finite")
            if not 0.0 <= number <= _MAXIMUM:
                raise ValueError(f"{MATERIAL_RATE_FIELD}.{name} must be between 0.0 and {_MAXIMUM:g}")
            rates[name] = number
        return rates

    @model_validator(mode="after")
    def _energy_order(self) -> "IonBeamParams":
        """Reject the native IBE energy order that flips the etch sign.

        ``ps.IonBeamEtching`` divides by
        ``sqrt(meanEnergy) - sqrt(thresholdEnergy)``. At
        ``thresholdEnergy >= meanEnergy`` that normalization changes sign, so
        the "etch" grows the surface, aborts the ray trace when the energies are
        equal with ``sigmaEnergy > 0``, or empties the top level set when
        ``sigmaEnergy == 0``. The worker and the API reject the same
        combination before any native object exists.
        """
        if self.thresholdEnergy >= self.meanEnergy:
            raise ValueError(
                f"thresholdEnergy ({self.thresholdEnergy:g}) must be less than meanEnergy ({self.meanEnergy:g}); "
                "ViennaPS compares the sampled energy with thresholdEnergy when it normalizes the "
                "IonBeamEtching yield, and thresholdEnergy >= meanEnergy reverses the native flux sign so an "
                "etch grows the surface"
            )
        return self

    def flat_values(self) -> dict[str, float]:
        """Return the seventeen scalar values keyed by their native name."""
        return {name: float(getattr(self, name)) for name in SCALAR_FIELDS}

    def native_scalar_values(self) -> dict[str, float]:
        """Return the values assigned directly to ``IBEParameters`` attributes."""
        return {name: float(getattr(self, name)) for name in SCALAR_FIELDS if not name.startswith("cos4Yield")}

    def cos4_yield_values(self) -> dict[str, float]:
        """Return ``cos4Yield.a1 .. a4`` in native member order."""
        return {f"a{index}": float(getattr(self, f"cos4YieldA{index}")) for index in range(1, 5)}

    def material_rates(self) -> dict[str, float]:
        """Return the per-material table, sorted for a stable record."""
        return {name: float(value) for name, value in sorted(self.materialPlaneWaferRateByMaterial.items())}

    def metadata_values(self) -> dict[str, Any]:
        """Return every parameter as a JSON-safe, stable mapping."""
        values: dict[str, Any] = self.flat_values()
        values[MATERIAL_RATE_FIELD] = self.material_rates()
        return values
