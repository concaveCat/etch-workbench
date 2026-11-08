"""Native ``NetDoping`` / ``SheetResistance`` field variant."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from process_models.v2.cellset import CELLSET_CONVERSION
from process_models.v2.contract import (
    EngineVariant,
    Issue,
    ProcessContext,
    StateProfile,
    VariantCapabilities,
)
from process_models.v2.engine import EngineLease, EngineRequirement, ExecutionProfile
from process_models.v2.engines import viennaps as _engine_helpers
from process_models.v2.state import (
    MetrologyRecord,
    OutputSet,
    ProcessState,
    StateDelta,
    StepResult,
)

from ..params import AnalysisParams

__all__ = ["AnalysisNativeVariant"]

#: One project length unit (um) in the native resistance model's centimetres.
_LENGTH_UNIT_CM = 1e-4


class AnalysisNativeVariant(EngineVariant):
    """Measure junction depth and sheet resistance on the existing cell set."""

    variant_id = "native"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry", "fields"}),
        writes=frozenset({"fields"}),
        supports_masks=False,
        mutates_input=True,
        determinism="strict",
        output_kinds=("metrology", "artifact:profile"),
        state_profile=StateProfile(conversions=(CELLSET_CONVERSION,)),
    )
    engine = EngineRequirement(
        kind="native",
        engine_id="viennaps",
        min_version="4.7.0",
        profiles=(ExecutionProfile(backend="cpu", kernel="CPU_DISK"),),
    )
    implementation_hash: str | None = None
    implementation_files = (
        ("process_models/v2/engines/viennaps.py", getattr(_engine_helpers, "__file__", "")),
        (
            "process_models/v2/engines/viennaps_cellset.py",
            __import__("process_models.v2.engines.viennaps_cellset", fromlist=["__file__"]).__file__,
        ),
    )

    def validate(self, ctx: ProcessContext, params: AnalysisParams) -> list[Issue]:
        """Return issues outside the canonical parameter model."""
        return _periodic_issue(ctx, bool(params.cellSet.periodic))

    def execute(
        self,
        state: ProcessState,
        params: AnalysisParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Run native analysis; fields stay in state, measurements go to outputs."""
        del ctx
        fields = state.require("fields")
        if fields.handle is None:
            raise RuntimeError("analysis requires a rehydrated native DenseCellSet")
        ps = engine.handle
        cell_set = fields.handle

        known = {str(label) for label in cell_set.getScalarDataLabels()}
        metadata: dict[str, Any] = {
            "nativeModel": None,
            "cellSet": {"numberOfCells": int(cell_set.getNumberOfCells()), "reused": True},
        }
        metrology: list[MetrologyRecord] = []

        net = params.netDoping
        if net is not None:
            inputs = [*net.donorLabels, *net.acceptorLabels]
            missing = [label for label in inputs if label not in known]
            if missing:
                raise RuntimeError("native NetDoping input field(s) are missing: " + ", ".join(missing))
            doping = ps.NetDoping()
            doping.setCellSet(cell_set)
            for label in net.donorLabels:
                doping.addDonorLabel(str(label))
            for label in net.acceptorLabels:
                doping.addAcceptorLabel(str(label))
            doping.setOutputLabel(str(net.outputLabel))
            if net.surfacePosition is not None:
                doping.setSurfacePosition(float(net.surfacePosition))
            doping.apply()
            if net.outputLabel not in {str(label) for label in cell_set.getScalarDataLabels()}:
                raise RuntimeError(f"native NetDoping did not write {net.outputLabel!r}")
            known.add(str(net.outputLabel))

            values = [float(value) for value in cell_set.getScalarData(net.outputLabel)]
            junction_depth = _finite_or_none(doping.junctionDepth())
            junction_depths = [_finite_or_none(value) for value in doping.junctionDepths()]
            lateral = None
            if params.scanDepth is not None:
                lateral = _finite_or_none(doping.lateralJunctionPosition(float(params.scanDepth)))
            metadata["nativeModel"] = "viennaps.NetDoping"
            metadata["netDoping"] = {
                "donorLabels": list(net.donorLabels),
                "acceptorLabels": list(net.acceptorLabels),
                "outputLabel": net.outputLabel,
                "surfacePosition": (float(net.surfacePosition) if net.surfacePosition is not None else 0.0),
                "surfacePositionSource": ("request" if net.surfacePosition is not None else "native-default-0"),
                "junctionDepth": junction_depth,
                "junctionDepths": junction_depths,
                "junctionCount": int(doping.junctionCount()),
                "scanDepth": float(params.scanDepth) if params.scanDepth is not None else None,
                "lateralJunctionPosition": lateral,
                "outputField": {
                    "count": len(values),
                    "min": min(values) if values else None,
                    "max": max(values) if values else None,
                    "sum": sum(values),
                },
            }
            if junction_depth is not None:
                metrology.append(
                    MetrologyRecord(
                        metric="junctionDepth",
                        value=junction_depth,
                        unit="um",
                        method="viennaps.NetDoping",
                    )
                )
            if lateral is not None:
                metrology.append(
                    MetrologyRecord(
                        metric="lateralJunctionPosition",
                        value=lateral,
                        unit="um",
                        region=f"scanDepth={float(params.scanDepth):g}",
                        method="viennaps.NetDoping",
                    )
                )

        sheet_rows: list[dict[str, Any]] = []
        for entry in params.sheetResistance:
            label = str(entry.concentrationLabel)
            if label not in known:
                raise RuntimeError(f"native SheetResistance input field {label!r} is missing")
            resistance = ps.SheetResistance()
            resistance.setCellSet(cell_set)
            resistance.setLengthUnit(_LENGTH_UNIT_CM)
            resistance.setConcentrationLabel(label)
            if entry.surfacePosition is not None:
                resistance.setSurfacePosition(float(entry.surfacePosition))
            value = resistance.computeElectron() if entry.carrier == "electron" else resistance.computeHole()
            finite = _finite_or_none(value)
            row = {
                "concentrationLabel": label,
                "carrier": entry.carrier,
                "nativeMethod": (
                    "viennaps.SheetResistance.computeElectron"
                    if entry.carrier == "electron"
                    else "viennaps.SheetResistance.computeHole"
                ),
                "lengthUnitCm": _LENGTH_UNIT_CM,
                "concentrationUnitCm3": 1e12,
                "surfacePosition": (float(entry.surfacePosition) if entry.surfacePosition is not None else 0.0),
                "surfacePositionSource": ("request" if entry.surfacePosition is not None else "native-default-0"),
                "sheetResistanceOhmPerSquare": finite,
            }
            sheet_rows.append(row)
            if finite is not None:
                metrology.append(
                    MetrologyRecord(
                        metric="sheetResistance",
                        value=finite,
                        unit="ohm/sq",
                        region=label,
                        method="viennaps.SheetResistance",
                    )
                )
        if sheet_rows:
            metadata["sheetResistance"] = sheet_rows
            if metadata["nativeModel"] is None:
                metadata["nativeModel"] = "viennaps.SheetResistance"

        metadata["fields"] = sorted(known)
        return StepResult(
            delta=StateDelta(fields=fields),
            outputs=OutputSet(metrology=tuple(metrology)),
            metadata=metadata,
        )


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _periodic_issue(ctx: ProcessContext, periodic: bool) -> list[Issue]:
    if not periodic:
        return []
    domain = ctx.project.get("domain") if isinstance(ctx.project, Mapping) else None
    if not isinstance(domain, Mapping):
        return []
    boundary = domain.get("xBoundary") or domain.get("boundaryXY")
    if boundary == "periodic":
        return []
    return [
        Issue(
            path="cellSet.periodic",
            message="cellSet.periodic=True requires a periodic project boundary",
        )
    ]
