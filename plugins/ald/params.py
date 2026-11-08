"""Canonical parameters for the native single-particle ALD plugin.

The twelve flat fields, their defaults, and their ranges mirror
``backend.specialized_models`` for the ``ald`` catalog row. They are repeated
here instead of imported so that importing the plugin never imports
:mod:`backend` (the plugin must load without the worker or the API layer).

The fields split into the two native objects the model needs:

``SingleParticleALDParams``
    ``stickingProbability``, ``gasMeanFreePath``, ``growthPerCycle``,
    ``incomingFlux``, ``evaporationFlux``, ``s0`` and
    ``coverageDiffusionCoefficient``.
``AtomicLayerProcessParameters``
    ``pulseTime``, ``purgePulseTime``, ``coverageTimeStep``, ``purgeTimeStep``
    and the integer ``numCycles``.

Every value must be finite. Sticking probabilities (``stickingProbability``
and ``s0``) are in ``[0, 1]``, ``gasMeanFreePath`` is ``>= -1`` (a negative
mean free path is the native "no gas scattering" sentinel), and all rates and
times are non-negative. ``numCycles`` is a plain integer in ``[1, 10000]``.

``s0`` is the initial sticking coefficient. The catalog exposes it with the
generic ``1e6`` numeric ceiling, but it is a probability: this model bounds it
to ``[0, 1]`` so the native solver never receives an unphysical value.

``depositMaterial`` is required because ``SingleParticleALD`` has no output
material of its own: the worker creates the step's material with
``Domain.duplicateTopLevelSet(depositMaterial)`` before ``Process.apply()``, so
the chosen material *is* the deposited level set.

``surfaceDiffusion`` is deliberately not part of this parameter model. The
4.7.0 native surface-diffusion stencil can hang, so steps that carry it are not
claimed by this plugin (see :mod:`plugins.ald.step`).
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ATOMIC_FIELDS",
    "FLAT_FIELDS",
    "NUM_CYCLES_FIELD",
    "ALDParams",
]

#: Native member order of ``ps.SingleParticleALDParams``.
FLAT_FIELDS: tuple[str, ...] = (
    "stickingProbability",
    "gasMeanFreePath",
    "growthPerCycle",
    "incomingFlux",
    "evaporationFlux",
    "s0",
    "coverageDiffusionCoefficient",
)

#: Native member order of ``ps.AtomicLayerProcessParameters`` (without cycles).
ATOMIC_FIELDS: tuple[str, ...] = (
    "pulseTime",
    "purgePulseTime",
    "coverageTimeStep",
    "purgeTimeStep",
)

NUM_CYCLES_FIELD = "numCycles"

_MAXIMUM = 1_000_000.0

_DEFAULTS: dict[str, float] = {
    "stickingProbability": 0.5,
    "gasMeanFreePath": 1.0,
    "growthPerCycle": 0.02,
    "incomingFlux": 1.0,
    "evaporationFlux": 0.1,
    "s0": 1.0,
    "coverageDiffusionCoefficient": 0.0,
    "pulseTime": 0.02,
    "purgePulseTime": 0.0,
    "coverageTimeStep": 0.01,
    "purgeTimeStep": 1.0,
}

_MINIMUMS: dict[str, float] = {name: 0.0 for name in FLAT_FIELDS + ATOMIC_FIELDS}
_MINIMUMS["gasMeanFreePath"] = -1.0
_MINIMUMS["stickingProbability"] = 0.0
_MINIMUMS["s0"] = 0.0

_MAXIMUMS: dict[str, float] = {name: _MAXIMUM for name in FLAT_FIELDS + ATOMIC_FIELDS}
_MAXIMUMS["stickingProbability"] = 1.0
_MAXIMUMS["s0"] = 1.0

_NUM_CYCLES_MIN = 1
_NUM_CYCLES_MAX = 10_000


class ALDParams(BaseModel):
    """Parameters of one native ``ps.SingleParticleALD`` step.

    Every numeric field must be finite; the ranges are the catalog's own (with
    ``s0`` tightened to the probability interval) so a step accepted by the
    project validator is accepted here and vice versa. ``depositMaterial`` must
    be a non-empty material name; it is looked up in the installed native
    ``MaterialRegistry`` at execute time, so an unknown name fails loudly
    instead of silently substituting a default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    duration: float = Field(gt=0)
    stickingProbability: float = Field(
        default=_DEFAULTS["stickingProbability"],
        ge=_MINIMUMS["stickingProbability"],
        le=_MAXIMUMS["stickingProbability"],
    )
    gasMeanFreePath: float = Field(
        default=_DEFAULTS["gasMeanFreePath"],
        ge=_MINIMUMS["gasMeanFreePath"],
        le=_MAXIMUMS["gasMeanFreePath"],
    )
    growthPerCycle: float = Field(
        default=_DEFAULTS["growthPerCycle"],
        ge=_MINIMUMS["growthPerCycle"],
        le=_MAXIMUMS["growthPerCycle"],
    )
    incomingFlux: float = Field(
        default=_DEFAULTS["incomingFlux"],
        ge=_MINIMUMS["incomingFlux"],
        le=_MAXIMUMS["incomingFlux"],
    )
    evaporationFlux: float = Field(
        default=_DEFAULTS["evaporationFlux"],
        ge=_MINIMUMS["evaporationFlux"],
        le=_MAXIMUMS["evaporationFlux"],
    )
    s0: float = Field(default=_DEFAULTS["s0"], ge=_MINIMUMS["s0"], le=_MAXIMUMS["s0"])
    coverageDiffusionCoefficient: float = Field(
        default=_DEFAULTS["coverageDiffusionCoefficient"],
        ge=_MINIMUMS["coverageDiffusionCoefficient"],
        le=_MAXIMUMS["coverageDiffusionCoefficient"],
    )
    pulseTime: float = Field(default=_DEFAULTS["pulseTime"], ge=_MINIMUMS["pulseTime"], le=_MAXIMUMS["pulseTime"])
    purgePulseTime: float = Field(
        default=_DEFAULTS["purgePulseTime"], ge=_MINIMUMS["purgePulseTime"], le=_MAXIMUMS["purgePulseTime"]
    )
    coverageTimeStep: float = Field(
        default=_DEFAULTS["coverageTimeStep"], ge=_MINIMUMS["coverageTimeStep"], le=_MAXIMUMS["coverageTimeStep"]
    )
    purgeTimeStep: float = Field(
        default=_DEFAULTS["purgeTimeStep"], ge=_MINIMUMS["purgeTimeStep"], le=_MAXIMUMS["purgeTimeStep"]
    )
    numCycles: int = Field(default=1, ge=_NUM_CYCLES_MIN, le=_NUM_CYCLES_MAX)
    depositMaterial: str = Field(min_length=1)

    @field_validator(*FLAT_FIELDS, *ATOMIC_FIELDS, "duration")
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
        """Return the seven values in ``ps.SingleParticleALDParams`` order."""
        return tuple(float(getattr(self, name)) for name in FLAT_FIELDS)

    def flat_values(self) -> dict[str, float]:
        """Return the seven values keyed by their native parameter name."""
        return {name: float(getattr(self, name)) for name in FLAT_FIELDS}

    def atomic_values(self) -> dict[str, Any]:
        """Return the atomic-layer parameters, cycles included as an ``int``."""
        values: dict[str, Any] = {name: float(getattr(self, name)) for name in ATOMIC_FIELDS}
        values[NUM_CYCLES_FIELD] = int(self.numCycles)
        return values
