"""Canonical parameters for the native cell-set analysis plugin."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["AnalysisParams", "CellSetParams", "NetDopingParams", "SheetResistanceEntry"]

_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,47}")
_RESERVED = frozenset({"Material", "FillingFraction"})


class CellSetParams(BaseModel):
    """The native cell-set lattice settings carried by a field step."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    height: float = Field(gt=0, le=1000)
    substrateDepth: float | None = Field(default=None, gt=0, le=1000)
    coverMaterial: str = Field(default="GAS", min_length=1, max_length=64)
    periodic: bool = False
    format: Literal["vtu", "json", "both"] = "both"
    scalarLabels: list[str] | None = None

    @model_validator(mode="after")
    def check_settings(self) -> "CellSetParams":
        if self.coverMaterial != "GAS":
            raise ValueError(
                "cellSet.coverMaterial must be GAS for implant/anneal/analysis: the native "
                "DenseCellSet generator treats the cover material as the phase above the "
                "surface and would void a target with the same material"
            )
        if self.scalarLabels is not None:
            if not self.scalarLabels:
                raise ValueError("cellSet.scalarLabels must be omitted or a non-empty list")
            if len(set(self.scalarLabels)) != len(self.scalarLabels):
                raise ValueError("cellSet.scalarLabels must not contain duplicates")
            for label in self.scalarLabels:
                if _LABEL.fullmatch(label) is None:
                    raise ValueError(f"cellSet.scalarLabels entry {label!r} is not a valid cell-set label")
                if label in _RESERVED:
                    raise ValueError(f"cellSet.scalarLabels entry {label!r} is reserved by the native cell set")
        return self


class NetDopingParams(BaseModel):
    """One native ``NetDoping`` evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    donorLabels: list[str] = Field(default_factory=list, max_length=8)
    acceptorLabels: list[str] = Field(default_factory=list, max_length=8)
    outputLabel: str = Field(default="net_doping", min_length=1, max_length=48)
    surfacePosition: float | None = None

    @model_validator(mode="after")
    def check_net_doping(self) -> "NetDopingParams":
        if not self.donorLabels and not self.acceptorLabels:
            raise ValueError("netDoping requires at least one donor or acceptor label")
        for name in (self.outputLabel, *self.donorLabels, *self.acceptorLabels):
            if _LABEL.fullmatch(name) is None:
                raise ValueError(f"{name!r} is not a valid cell-set label")
            if name in _RESERVED:
                raise ValueError(f"netDoping label {name!r} is reserved by the native cell set")
        labels = [*self.donorLabels, *self.acceptorLabels]
        if len(set(labels)) != len(labels):
            raise ValueError("netDoping labels must be unique across donors and acceptors")
        if self.outputLabel in labels:
            raise ValueError("netDoping.outputLabel must not overwrite an input field")
        return self


class SheetResistanceEntry(BaseModel):
    """One native ``SheetResistance`` evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    concentrationLabel: str = Field(min_length=1, max_length=48)
    carrier: Literal["electron", "hole"]
    surfacePosition: float | None = None

    @model_validator(mode="after")
    def check_label(self) -> "SheetResistanceEntry":
        if _LABEL.fullmatch(self.concentrationLabel) is None:
            raise ValueError("concentrationLabel is not a valid cell-set label")
        if self.concentrationLabel in _RESERVED:
            raise ValueError("concentrationLabel is reserved by the native cell set")
        return self


class AnalysisParams(BaseModel):
    """Parameters of one native ``NetDoping`` / ``SheetResistance`` step."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    netDoping: NetDopingParams | None = None
    sheetResistance: list[SheetResistanceEntry] = Field(default_factory=list, max_length=8)
    scanDepth: float | None = Field(default=None, ge=0, le=10000)
    cellSet: CellSetParams

    @model_validator(mode="after")
    def check_analysis_fields(self) -> "AnalysisParams":
        if self.netDoping is None and not self.sheetResistance:
            raise ValueError("analysis requires netDoping or at least one sheetResistance entry")
        if self.scanDepth is not None and self.netDoping is None:
            raise ValueError("scanDepth is the NetDoping lateral scan and requires netDoping")
        return self
