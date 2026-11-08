"""Canonical parameters for the template process."""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["ExampleParams"]


class ExampleParams(BaseModel):
    """Validated parameters for one template step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rate: float = Field(gt=0)
    duration: float = Field(gt=0)

    @field_validator("rate", "duration")
    @classmethod
    def _finite(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value
