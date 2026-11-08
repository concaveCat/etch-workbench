"""Native ``IonImplantation`` field variant.

The variant consumes the native cell set derived on demand by the cell-set
backend. It never calls ``generateCellSet`` itself: generation is a sequence
boundary cost owned by the backend, not a per-step operation.
"""

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

from ..params import ImplantParams, ManualImplantProfile

__all__ = ["ImplantNativeVariant"]

#: One project length unit (um) in the native implant model's centimetres.
_LENGTH_UNIT_CM = 1e-4


class ImplantNativeVariant(EngineVariant):
    """Run one native ``IonImplantation`` process on the field cell set."""

    variant_id = "native"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry", "fields"}),
        writes=frozenset({"fields"}),
        supports_masks=True,
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

    def validate(self, ctx: ProcessContext, params: ImplantParams) -> list[Issue]:
        """Return issues the canonical parameter model cannot express."""
        return _periodic_issue(ctx, bool(params.cellSet.periodic))

    def execute(
        self,
        state: ProcessState,
        params: ImplantParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Stamp concentration/damage fields into the existing native cell set."""
        del ctx
        fields = state.require("fields")
        if fields.handle is None or fields.domain_handle is None:
            raise RuntimeError("implant requires a rehydrated native DenseCellSet")
        ps = engine.handle
        cell_set = fields.handle
        field_domain = fields.domain_handle

        profile_model, profile_record = _build_profile(ps, params.profile)
        implant = ps.IonImplantation()
        implant.setImplantModel(profile_model)
        damage_record = _configure_damage(ps, params, implant)

        implant.setDose(float(params.doseCm2))
        implant.setTiltAngle(float(params.tiltDeg))
        implant.setLengthUnit(_LENGTH_UNIT_CM)
        implant.setDoseControl(getattr(ps.ImplantDoseControl, str(params.doseControl)))
        implant.setMaskMaterials([material_handle(ps, name) for name in params.maskMaterials])
        implant.setScreenMaterials([material_handle(ps, name) for name in params.screenMaterials])

        labels = params.labels
        if labels is None or labels.concentration is None:
            raise RuntimeError("implant labels were not resolved")
        implant.setConcentrationLabel(str(labels.concentration))
        if labels.damage:
            implant.setDamageLabel(str(labels.damage))
        if labels.lastDamage:
            implant.setLastDamageLabel(str(labels.lastDamage))
        if float(params.damageFactor) != 1.0:
            implant.setDamageFactor(float(params.damageFactor))

        process = ps.Process(field_domain, implant, 0.0)
        process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
        process.apply()

        known = {str(label) for label in cell_set.getScalarDataLabels()}
        expected = [str(labels.concentration)] + [str(value) for value in (labels.damage, labels.lastDamage) if value]
        missing = [label for label in expected if label not in known]
        if missing:
            raise RuntimeError("native IonImplantation did not write cell-set field(s): " + ", ".join(missing))

        metadata: dict[str, Any] = {
            "nativeModel": "viennaps.IonImplantation",
            "nativeProcess": "ps.Process(fieldDomain, model, 0.0)",
            "requested": {
                "species": params.species,
                "material": params.material,
                "energyKeV": float(params.energyKeV),
                "doseCm2": float(params.doseCm2),
                "tiltDeg": float(params.tiltDeg),
                "rotationDeg": float(params.rotationDeg),
                "doseControl": params.doseControl,
                "maskMaterials": list(params.maskMaterials),
                "screenMaterials": list(params.screenMaterials),
            },
            "effective": {
                "doseCm2": float(params.doseCm2),
                "tiltDeg": float(params.tiltDeg),
                "lengthUnitCm": _LENGTH_UNIT_CM,
                "doseControl": params.doseControl,
                "damageFactor": float(params.damageFactor),
                "concentrationLabel": str(labels.concentration),
                "damageLabel": labels.damage,
                "lastDamageLabel": labels.lastDamage,
            },
            "energyKeVNativeEffect": False,
            "rotationDegNativeEffect": False,
            "profile": profile_record,
            "damage": damage_record,
            "cellSet": {
                "grid": {
                    "dimension": fields.grid.dimension,
                    "origin": list(fields.grid.origin),
                    "spacing": fields.grid.spacing,
                    "shape": list(fields.grid.shape),
                },
                "numberOfCells": int(cell_set.getNumberOfCells()),
                "reused": True,
            },
            "fields": sorted(known),
        }
        return StepResult(delta=StateDelta(fields=fields), metadata=metadata)


def _build_profile(ps: Any, profile: ManualImplantProfile) -> tuple[Any, dict[str, Any]]:
    """Construct the native profile model and return a JSON-safe readback."""

    def moments(row: Any) -> tuple[Any, dict[str, float]]:
        parameters = ps.PearsonIVParameters()
        parameters.mu = float(row.mu)
        parameters.sigma = float(row.sigma)
        parameters.beta = float(row.beta)
        parameters.gamma = float(row.gamma)
        return parameters, {
            "mu": float(parameters.mu),
            "sigma": float(parameters.sigma),
            "beta": float(parameters.beta),
            "gamma": float(parameters.gamma),
        }

    head, head_record = moments(profile.head)
    record: dict[str, Any] = {
        "source": "manual",
        "kind": profile.kind,
        "head": head_record,
        "lateralMu": float(profile.lateralMu),
        "lateralSigma": float(profile.lateralSigma),
    }
    if profile.kind == "pearsonIV":
        model = ps.ImplantPearsonIV(head, record["lateralMu"], record["lateralSigma"])
        record["nativeConstructor"] = "viennaps.ImplantPearsonIV"
    elif profile.kind == "dualPearsonIV":
        if profile.tail is None:
            raise RuntimeError("dualPearsonIV is missing its tail moments")
        tail, tail_record = moments(profile.tail)
        record.update(
            {
                "tail": tail_record,
                "headFraction": float(profile.headFraction),
                "tailLateralMu": float(profile.tailLateralMu),
                "tailLateralSigma": float(profile.tailLateralSigma),
            }
        )
        model = ps.ImplantDualPearsonIV(
            head,
            tail,
            record["headFraction"],
            record["lateralMu"],
            record["lateralSigma"],
            record["tailLateralMu"],
            record["tailLateralSigma"],
        )
        record["nativeConstructor"] = "viennaps.ImplantDualPearsonIV"
    else:
        record.update(
            {
                "tailFraction": float(profile.tailFraction),
                "tailStartDepth": float(profile.tailStartDepth),
                "tailDecayLength": float(profile.tailDecayLength),
                "tailBlendWidth": float(profile.tailBlendWidth),
            }
        )
        model = ps.ImplantPearsonIVChanneling(
            head,
            record["lateralMu"],
            record["lateralSigma"],
            record["tailFraction"],
            record["tailStartDepth"],
            record["tailDecayLength"],
            record["tailBlendWidth"],
        )
        record["nativeConstructor"] = "viennaps.ImplantPearsonIVChanneling"
    return model, record


def _configure_damage(ps: Any, params: ImplantParams, implant: Any) -> dict[str, Any] | None:
    damage = params.damage
    if damage is None:
        return None
    model = ps.ImplantDamageHobler(
        float(damage.projectedRange),
        float(damage.verticalSigma),
        float(damage.lambdaValue),
        float(damage.defectsPerIon),
        float(damage.lateralSigma),
        float(damage.lateralDeltaSigma),
    )
    implant.setDamageModel(model)
    return {
        "nativeConstructor": "viennaps.ImplantDamageHobler",
        "kind": damage.kind,
        "projectedRange": float(damage.projectedRange),
        "verticalSigma": float(damage.verticalSigma),
        "lambdaValue": float(damage.lambdaValue),
        "defectsPerIon": float(damage.defectsPerIon),
        "lateralSigma": float(damage.lateralSigma),
        "lateralDeltaSigma": float(damage.lateralDeltaSigma),
    }


def _periodic_issue(ctx: ProcessContext, periodic: bool) -> list[Issue]:
    """Reject a periodic cell set on a non-periodic project domain."""
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
