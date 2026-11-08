"""Native ``Anneal`` field variant."""

from __future__ import annotations

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
from process_models.v2.engines.viennaps import material_handle
from process_models.v2.state import ProcessState, StateDelta, StepResult

from ..params import AnnealParams

__all__ = ["AnnealNativeVariant"]


class AnnealNativeVariant(EngineVariant):
    """Diffuse and activate dopants in the existing native cell set."""

    variant_id = "native"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry", "fields"}),
        writes=frozenset({"fields"}),
        supports_masks=False,
        mutates_input=True,
        determinism="strict",
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

    def validate(self, ctx: ProcessContext, params: AnnealParams) -> list[Issue]:
        """Return issues outside the canonical parameter model."""
        return _periodic_issue(ctx, bool(params.cellSet.periodic))

    def execute(
        self,
        state: ProcessState,
        params: AnnealParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Run one native anneal on the existing cell set."""
        del ctx
        fields = state.require("fields")
        if fields.handle is None or fields.domain_handle is None:
            raise RuntimeError("anneal requires a rehydrated native DenseCellSet")
        ps = engine.handle
        cell_set = fields.handle
        field_domain = fields.domain_handle

        known = {str(label) for label in cell_set.getScalarDataLabels()}
        if params.speciesLabel not in known:
            raise RuntimeError(
                f"anneal speciesLabel {params.speciesLabel!r} is not present in the native cell set; "
                "an earlier implant step must produce it"
            )

        anneal = ps.Anneal()
        schedule_record = _configure_schedule(ps, anneal, params)
        diffusivity_record = _configure_diffusivity(anneal, params)
        anneal.setMode(getattr(ps.AnnealMode, str(params.mode)))
        solver_record: dict[str, Any] = {
            "mode": params.mode,
            "nativeCall": f"setMode(viennaps.AnnealMode.{params.mode})",
        }
        if params.implicitSolver is not None:
            options = params.implicitSolver
            anneal.setImplicitSolverOptions(
                int(options.maxIterations),
                float(options.relativeTolerance),
                float(options.relaxation),
            )
            solver_record["implicitSolver"] = {
                "maxIterations": int(options.maxIterations),
                "relativeTolerance": float(options.relativeTolerance),
                "relaxation": float(options.relaxation),
            }
        if params.timeStep is not None:
            anneal.setTimeStep(float(params.timeStep))
            solver_record["timeStep"] = float(params.timeStep)
        if params.stabilityFactor is not None:
            anneal.setStabilityFactor(float(params.stabilityFactor))
            solver_record["stabilityFactor"] = float(params.stabilityFactor)

        anneal.setDiffusionMaterials([material_handle(ps, name) for name in params.diffusionMaterials])
        anneal.setBlockingMaterials([material_handle(ps, name) for name in params.blockingMaterials])
        anneal.setSpeciesLabel(str(params.speciesLabel))
        anneal.setActiveLabel(str(params.activeLabel))
        activation = params.solidActivation
        anneal.enableSolidActivation(True)
        anneal.setSolidSolubilityArrhenius(
            float(activation.solubilityC0),
            float(activation.solubilityEa_eV),
        )

        if params.activationOnly:
            anneal.applyActivation(field_domain)
            native_call = "Anneal.applyActivation(fieldDomain)"
        else:
            process = ps.Process(field_domain, anneal, 0.0)
            process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
            process.apply()
            native_call = "ps.Process(fieldDomain, model, 0.0)"

        known = {str(label) for label in cell_set.getScalarDataLabels()}
        if params.activeLabel not in known:
            raise RuntimeError(f"native Anneal did not write the active-concentration field {params.activeLabel!r}")

        species_values = [float(value) for value in cell_set.getScalarData(params.speciesLabel)]
        active_values = [float(value) for value in cell_set.getScalarData(params.activeLabel)]
        total = sum(species_values)
        metadata: dict[str, Any] = {
            "nativeModel": "viennaps.Anneal",
            "nativeProcess": native_call,
            "activationOnly": bool(params.activationOnly),
            "requested": {
                "mode": params.mode,
                "speciesLabel": params.speciesLabel,
                "activeLabel": params.activeLabel,
                "diffusionMaterials": list(params.diffusionMaterials),
                "blockingMaterials": list(params.blockingMaterials),
            },
            "schedule": schedule_record,
            "diffusivity": diffusivity_record,
            "solver": solver_record,
            "solidActivation": {
                "solubilityC0": float(activation.solubilityC0),
                "solubilityEa_eV": float(activation.solubilityEa_eV),
                "nativeCall": "enableSolidActivation + setSolidSolubilityArrhenius",
            },
            "activation": {
                "speciesLabel": params.speciesLabel,
                "activeLabel": params.activeLabel,
                "totalConcentrationSum": total,
                "activeConcentrationSum": sum(active_values),
                "activeFractionOfTotal": (sum(active_values) / total) if total > 0.0 else None,
                "note": "sums are over every native cell, not a local activation model",
            },
            "cellSet": {
                "numberOfCells": int(cell_set.getNumberOfCells()),
                "reused": True,
            },
        }
        return StepResult(delta=StateDelta(fields=fields), metadata=metadata)


def _configure_schedule(ps: Any, anneal: Any, params: AnnealParams) -> dict[str, Any]:
    schedule = params.schedule
    if schedule.kind == "isothermal":
        anneal.setTemperature(float(schedule.temperatureK))
        anneal.setDuration(float(schedule.durationSeconds))
        return {
            "kind": "isothermal",
            "temperatureK": float(schedule.temperatureK),
            "durationSeconds": float(schedule.durationSeconds),
            "nativeCall": "setTemperature + setDuration",
        }
    durations = [float(value) for value in schedule.durations]
    temperatures = [float(value) for value in schedule.temperatures]
    anneal.setTemperatureSchedule(durations, temperatures)
    return {
        "kind": "schedule",
        "durations": durations,
        "temperatures": temperatures,
        "nativeCall": "setTemperatureSchedule",
    }


def _configure_diffusivity(anneal: Any, params: AnnealParams) -> dict[str, Any]:
    diffusivity = params.diffusivity
    if diffusivity.kind == "arrhenius":
        anneal.setArrheniusParameters(float(diffusivity.D0), float(diffusivity.Ea_eV))
        return {
            "kind": "arrhenius",
            "D0": float(diffusivity.D0),
            "Ea_eV": float(diffusivity.Ea_eV),
            "nativeCall": "setArrheniusParameters",
        }
    anneal.setDiffusionCoefficient(float(diffusivity.value))
    return {
        "kind": "constant",
        "value": float(diffusivity.value),
        "nativeCall": "setDiffusionCoefficient",
    }


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
