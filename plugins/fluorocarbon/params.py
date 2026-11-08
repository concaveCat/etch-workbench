"""Canonical parameters for the native fluorocarbon plasma-etch plugin.

``ps.FluorocarbonEtching`` is not a flat-argument constructor: it takes a
``ps.FluorocarbonParameters`` object holding eight process scalars, a nested
``Ions`` block of six values, and one ``ps.FluorocarbonMaterialParameters``
row per material involved in the etch. The catalog defaults and ranges below
mirror ``backend.specialized_models`` for the ``fluorocarbon`` row and
``worker/specialized.py``'s construction. They are repeated here instead of
imported so that importing the plugin never imports :mod:`backend` or the
worker (the plugin must load without the API or worker layer).

The scalar bounds are the platform's own: ``0 <= value <= 1e6`` except
``etchStopDepth``, whose documented floor is ``-1e6``. Material rows use the
table bounds ``-1e6 <= value <= 1e6`` for all ten fields.

``materialParametersByMaterial`` is a strict, material-keyed table. Every row
must carry exactly the ten native fields; :mod:`plugins.fluorocarbon.variants`
fills a *missing* row for a material that is referenced by the state with
:data:`FLUOROCARBON_MATERIAL_DEFAULTS`. The accepted material names are the
installed catalog plus the model's own ``Polymer`` pseudo-material, matching
``backend.validation``'s ``fluorocarbonMaterials`` rule. A name outside that
set or an unknown row key is a validation error, never a silent default.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "CATALOG_MATERIAL_NAMES",
    "FLUOROCARBON_MATERIAL_DEFAULTS",
    "FluorocarbonMaterialParams",
    "FluorocarbonParams",
    "ION_FIELDS",
    "MATERIAL_FIELDS",
    "MODEL_FIELDS",
    "POLYMER_MATERIAL",
    "SCALAR_FIELDS",
]

#: Top-level ``ps.FluorocarbonParameters`` fields, in worker assignment order.
MODEL_FIELDS: tuple[str, ...] = (
    "ionFlux",
    "etchantFlux",
    "polyFlux",
    "k_ie",
    "k_ev",
    "delta_p",
    "temperature",
    "etchStopDepth",
)

#: ``ps.FluorocarbonParameters.Ions`` fields, in worker assignment order.
ION_FIELDS: tuple[str, ...] = (
    "meanEnergy",
    "sigmaEnergy",
    "exponent",
    "n_l",
    "inflectAngle",
    "minAngle",
)

#: All process scalars in catalog order (``etchStopDepth`` is last there).
SCALAR_FIELDS: tuple[str, ...] = (
    "ionFlux",
    "etchantFlux",
    "polyFlux",
    "k_ie",
    "k_ev",
    "delta_p",
    "temperature",
    "meanEnergy",
    "sigmaEnergy",
    "exponent",
    "n_l",
    "inflectAngle",
    "minAngle",
    "etchStopDepth",
)

#: ``ps.FluorocarbonMaterialParameters`` fields. All ten are required.
MATERIAL_FIELDS: tuple[str, ...] = (
    "A_ie",
    "A_sp",
    "B_sp",
    "E_a",
    "Eth_ie",
    "Eth_sp",
    "K",
    "beta_e",
    "beta_p",
    "density",
)

#: Defaults used for a referenced material with no explicit row.
FLUOROCARBON_MATERIAL_DEFAULTS: dict[str, float] = {
    "A_ie": 0.0361,
    "A_sp": 0.0139,
    "B_sp": 9.3,
    "E_a": 0.168,
    "Eth_ie": 4.0,
    "Eth_sp": 18.0,
    "K": 0.002789491704544977,
    "beta_e": 0.9,
    "beta_p": 0.26,
    "density": 2.2,
}

#: Installed catalog materials (``docs/materials-catalog.json`` order). The
#: validation layer also accepts the model's ``Polymer`` pseudo-material.
CATALOG_MATERIAL_NAMES: tuple[str, ...] = (
    "Mask",
    "Si",
    "SiO2",
    "Si3N4",
    "SiN",
    "PolySi",
    "SiGe",
    "Al2O3",
    "HfO2",
    "TiN",
    "W",
    "Cu",
    "PMMA",
    "Metal",
    "Dielectric",
)

POLYMER_MATERIAL = "Polymer"
KNOWN_MATERIALS = frozenset(CATALOG_MATERIAL_NAMES) | {POLYMER_MATERIAL}

_MAXIMUM = 1_000_000.0

_DEFAULTS: dict[str, float] = {
    "ionFlux": 1.0,
    "etchantFlux": 1.0,
    "polyFlux": 0.2,
    "k_ie": 2.0,
    "k_ev": 2.0,
    "delta_p": 1.0,
    "temperature": 300.0,
    "meanEnergy": 100.0,
    "sigmaEnergy": 10.0,
    "exponent": 500.0,
    "n_l": 10.0,
    "inflectAngle": 1.55334303,
    "minAngle": 1.3962634,
    "etchStopDepth": -1_000_000.0,
}

_MINIMUMS: dict[str, float] = {name: 0.0 for name in SCALAR_FIELDS}
_MINIMUMS["etchStopDepth"] = -1_000_000.0


class FluorocarbonMaterialParams(BaseModel):
    """One ``ps.FluorocarbonMaterialParameters`` row.

    All ten fields are required and strict: a partial row would silently keep
    native defaults, which is exactly the ambiguity the explicit table is
    meant to remove.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    A_ie: float = Field(ge=-_MAXIMUM, le=_MAXIMUM)
    A_sp: float = Field(ge=-_MAXIMUM, le=_MAXIMUM)
    B_sp: float = Field(ge=-_MAXIMUM, le=_MAXIMUM)
    E_a: float = Field(ge=-_MAXIMUM, le=_MAXIMUM)
    Eth_ie: float = Field(ge=-_MAXIMUM, le=_MAXIMUM)
    Eth_sp: float = Field(ge=-_MAXIMUM, le=_MAXIMUM)
    K: float = Field(ge=-_MAXIMUM, le=_MAXIMUM)
    beta_e: float = Field(ge=-_MAXIMUM, le=_MAXIMUM)
    beta_p: float = Field(ge=-_MAXIMUM, le=_MAXIMUM)
    density: float = Field(ge=-_MAXIMUM, le=_MAXIMUM)

    @field_validator(*MATERIAL_FIELDS)
    @classmethod
    def _finite(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    def values(self) -> dict[str, float]:
        """Return the ten values keyed by their native parameter name."""
        return {name: float(getattr(self, name)) for name in MATERIAL_FIELDS}


class FluorocarbonParams(BaseModel):
    """Parameters of one native ``ps.FluorocarbonEtching`` step.

    Every numeric field must be finite; scalar ranges are the catalog's own so
    a step accepted by ``backend.validation`` is accepted here and vice versa.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    duration: float = Field(gt=0)
    ionFlux: float = Field(default=_DEFAULTS["ionFlux"], ge=_MINIMUMS["ionFlux"], le=_MAXIMUM)
    etchantFlux: float = Field(default=_DEFAULTS["etchantFlux"], ge=_MINIMUMS["etchantFlux"], le=_MAXIMUM)
    polyFlux: float = Field(default=_DEFAULTS["polyFlux"], ge=_MINIMUMS["polyFlux"], le=_MAXIMUM)
    k_ie: float = Field(default=_DEFAULTS["k_ie"], ge=_MINIMUMS["k_ie"], le=_MAXIMUM)
    k_ev: float = Field(default=_DEFAULTS["k_ev"], ge=_MINIMUMS["k_ev"], le=_MAXIMUM)
    delta_p: float = Field(default=_DEFAULTS["delta_p"], ge=_MINIMUMS["delta_p"], le=_MAXIMUM)
    temperature: float = Field(default=_DEFAULTS["temperature"], ge=_MINIMUMS["temperature"], le=_MAXIMUM)
    meanEnergy: float = Field(default=_DEFAULTS["meanEnergy"], ge=_MINIMUMS["meanEnergy"], le=_MAXIMUM)
    sigmaEnergy: float = Field(default=_DEFAULTS["sigmaEnergy"], ge=_MINIMUMS["sigmaEnergy"], le=_MAXIMUM)
    exponent: float = Field(default=_DEFAULTS["exponent"], ge=_MINIMUMS["exponent"], le=_MAXIMUM)
    n_l: float = Field(default=_DEFAULTS["n_l"], ge=_MINIMUMS["n_l"], le=_MAXIMUM)
    inflectAngle: float = Field(default=_DEFAULTS["inflectAngle"], ge=_MINIMUMS["inflectAngle"], le=_MAXIMUM)
    minAngle: float = Field(default=_DEFAULTS["minAngle"], ge=_MINIMUMS["minAngle"], le=_MAXIMUM)
    etchStopDepth: float = Field(default=_DEFAULTS["etchStopDepth"], ge=_MINIMUMS["etchStopDepth"], le=_MAXIMUM)
    materialParametersByMaterial: Mapping[str, FluorocarbonMaterialParams] = Field(default_factory=dict)

    @field_validator("duration", *SCALAR_FIELDS)
    @classmethod
    def _finite(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    @field_validator("materialParametersByMaterial")
    @classmethod
    def _known_materials(
        cls, value: Mapping[str, FluorocarbonMaterialParams]
    ) -> Mapping[str, FluorocarbonMaterialParams]:
        unknown = sorted(set(value) - KNOWN_MATERIALS)
        if unknown:
            raise ValueError(f"unknown material names {unknown}; expected catalog materials or Polymer")
        return value

    def scalar_values(self) -> dict[str, float]:
        """Return the fourteen scalars in catalog order."""
        return {name: float(getattr(self, name)) for name in SCALAR_FIELDS}

    def material_values(self) -> dict[str, dict[str, float]]:
        """Return the explicit material rows keyed by material name."""
        return {str(name): row.values() for name, row in self.materialParametersByMaterial.items()}
