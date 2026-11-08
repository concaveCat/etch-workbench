"""Canonical parameters for the native GeometricTrenchDeposition plugin.

The seven flat fields and their defaults mirror
``backend.specialized_models`` for the ``geometricTrenchDeposition`` catalog
row. They are repeated here instead of imported so that importing the plugin
never imports :mod:`backend` (the plugin must load without the worker or the
API layer). ``trenchWidth``, ``trenchDepth`` and ``depositionRate`` are
positive; ``bottomMed``, ``a`` and ``b`` are non-negative; ``n`` lies in
``[1, 100]``. Every value must be finite.

``depositMaterial`` is required because the native model has no material table:
the worker creates the step's output material with
``Domain.duplicateTopLevelSet(depositMaterial)`` before ``Process.apply()``, so
the chosen material *is* the deposited level set. The isotropic plugin keeps
the same field on its parameter model for the same reason.

The reach guard (``depositionRate >= max(a + b, bottomMed)``) is a semantic
rule, not a Pydantic range: it compares several fields at once. It is exposed
here so :mod:`plugins.geometricTrenchDeposition.step` and the native variant
share one implementation. A rate below the native radius leaves the deposited
level set empty and can SIGSEGV inside the geometric-advection kernel, so the
request must be rejected before the native constructor is called.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "FLAT_FIELDS",
    "GeometricTrenchDepositionParams",
    "REACH_GUARD_FORMULA",
]

#: Native constructor argument order of ``ps.GeometricTrenchDeposition``.
FLAT_FIELDS: tuple[str, ...] = (
    "trenchWidth",
    "trenchDepth",
    "depositionRate",
    "bottomMed",
    "a",
    "b",
    "n",
)

#: The amount of material the native kernel adds at the trench top (z = 0).
REACH_GUARD_FORMULA = "max(a + b, bottomMed)"

_DEFAULTS: dict[str, float] = {
    "trenchWidth": 1.0,
    "trenchDepth": 4.0,
    "depositionRate": 0.5,
    "bottomMed": 0.2,
    "a": 0.3,
    "b": 0.1,
    "n": 1.0,
}


class GeometricTrenchDepositionParams(BaseModel):
    """Parameters of one native ``ps.GeometricTrenchDeposition`` step.

    Every numeric field must be finite. ``depositMaterial`` must be a
    non-empty material name; it is looked up in the installed native
    ``MaterialRegistry`` at execute time, so an unknown name fails loudly
    instead of silently substituting a default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    duration: float = Field(gt=0)
    trenchWidth: float = Field(default=_DEFAULTS["trenchWidth"], gt=0)
    trenchDepth: float = Field(default=_DEFAULTS["trenchDepth"], gt=0)
    depositionRate: float = Field(default=_DEFAULTS["depositionRate"], gt=0)
    bottomMed: float = Field(default=_DEFAULTS["bottomMed"], ge=0.0)
    a: float = Field(default=_DEFAULTS["a"], ge=0.0)
    b: float = Field(default=_DEFAULTS["b"], ge=0.0)
    n: float = Field(default=_DEFAULTS["n"], ge=1.0, le=100.0)
    depositMaterial: str = Field(min_length=1)

    @field_validator(*FLAT_FIELDS, "duration")
    @classmethod
    def _finite(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    @field_validator("depositMaterial")
    @classmethod
    def _material_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("depositMaterial must be a non-empty material name")
        return value

    def flat_arguments(self) -> tuple[float, ...]:
        """Return the seven values in ``ps.GeometricTrenchDeposition`` order."""
        return tuple(float(getattr(self, name)) for name in FLAT_FIELDS)

    def flat_values(self) -> dict[str, float]:
        """Return the seven values keyed by their native parameter name."""
        return {name: float(getattr(self, name)) for name in FLAT_FIELDS}

    def reach_radius(self) -> float:
        """Return the native deposition reach this step requires."""
        return max(float(self.a) + float(self.b), float(self.bottomMed))

    def reach_guard_violation(self) -> str | None:
        """Return a rejection message when ``depositionRate`` is too small.

        The native ViennaLS distribution advertises ``+/-depositionRate`` as
        its reach but computes a local radius ``a*(1-|z|/trenchDepth)^n + b``
        (or ``bottomMed`` at the floor). When the rate is below that radius the
        deposited level set can end up empty and the kernel can abort with
        SIGSEGV, so this is a hard rejection and never a warning.
        """
        radius = self.reach_radius()
        if float(self.depositionRate) < radius - 1e-12:
            return (
                f"nativeParameters.depositionRate: must be at least the native radius ({radius:g} um) = "
                f"{REACH_GUARD_FORMULA}; the installed ViennaLS geometric advection window is derived "
                "from depositionRate, and a smaller rate leaves the deposited level set empty or "
                "aborts with SIGSEGV"
            )
        return None

    def reach_guard(self) -> Mapping[str, Any]:
        """Return the JSON-safe reach-guard record used in step metadata."""
        return {
            "formula": REACH_GUARD_FORMULA,
            "requiredRate": self.reach_radius(),
            "depositionRate": float(self.depositionRate),
            "satisfied": self.reach_guard_violation() is None,
        }
