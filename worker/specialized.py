"""Allowlisted ViennaPS 4.6.2 model constructors for schema-7 specialized steps."""
from __future__ import annotations

import math
from typing import Any

from backend.specialized_models import (IBE_ENERGY_MODELS, PLASMA_COVERAGE_MODELS, advanced_spec,
                                       coverage_tolerance_issues, faraday_cage_2d_engine_error,
                                       geometric_trench_issues, ibe_energy_issues,
                                       model_spec, nv_factors_issues,
                                       oxidation_advanced_spec)
from backend.engine import (ald_surface_diffusion_error, engine_version,
                           oxidation_advanced_supported)


def _set_native_path(target: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)


def _read_native_values(params: Any, spec: dict[str, Any], material_handle: Any,
                        material_names: list[str]) -> dict[str, Any]:
    """Read the installed parameter object back as plain JSON values."""
    values: dict[str, Any] = {"blocks": {}, "scalars": {}, "maps": {}}
    for name, block in spec["blocks"].items():
        native = getattr(params, block["native"])
        values["blocks"][name] = {
            field["key"]: (bool(getattr(native, field["key"])) if field["kind"] == "boolean"
                           else float(getattr(native, field["key"])))
            for field in block["fields"]
        }
    for field in spec["scalars"]:
        value = getattr(params, field["key"])
        values["scalars"][field["key"]] = value if field["kind"] == "boolean" else float(value)
    for row in spec["maps"]:
        native = getattr(params, row["native"])
        by_material: dict[str, float] = {}
        unresolved: list[str] = []
        for material_name in material_names:
            try:
                handle = material_handle(material_name)
            except Exception:
                unresolved.append(material_name)
                continue
            by_material[material_name] = float(native.get(handle))
        values["maps"][row["key"]] = {"default": float(native.getDefault()),
                                      "byMaterial": by_material}
        if unresolved:
            values["maps"][row["key"]]["unresolvedMaterialNames"] = unresolved
    return values


def _build_plasma_model(ps: Any, model_id: str, spec: dict[str, Any], parameters: dict[str, Any],
                        flat_keys: tuple[str, ...], material_handle: Any,
                        material_ids: dict[str, int]) -> tuple[Any, dict[str, Any] | None]:
    """Construct one of the four plasma models through the installed overloads.

    Without ``advanced`` this is exactly the pre-existing flat float overload.
    With ``advanced`` the flat model is built first, its model-owned
    ``getParameters()`` object is used as the base (preserving each model's
    native defaults such as HBrO2's Substrate values and SF6C4F8's
    non-passivating defaults), the flat values are applied explicitly, then
    the validated advanced overrides, and the parameter-object overload is
    called. Model-specific rejected keys are checked before any native call.
    """
    native_class = getattr(ps, spec["nativeClass"])
    flat_args = tuple(float(parameters[key]) for key in flat_keys)
    if "advanced" not in parameters:
        return native_class(*flat_args), None
    advanced_schema = advanced_spec(model_id)
    if advanced_schema is None:
        raise RuntimeError(f"{model_id} has no advanced native parameter schema")
    advanced = parameters["advanced"]
    if not isinstance(advanced, dict):
        raise RuntimeError(f"{model_id} advanced native parameters must be a JSON object")

    allowed_blocks = advanced_schema["blocks"]
    allowed_scalars = {field["key"]: field for field in advanced_schema["scalars"]}
    allowed_maps = {row["key"]: row for row in advanced_schema["maps"]}
    rejected = advanced_schema.get("rejectedKeys", {})
    if rejected_keys := sorted(set(advanced) & set(rejected)):
        raise RuntimeError(f"{model_id} advanced.{rejected_keys[0]} is not supported: "
                           f"{rejected[rejected_keys[0]]}")
    unknown = sorted(set(advanced) - set(allowed_blocks) - set(allowed_scalars) - set(allowed_maps))
    if unknown:
        raise RuntimeError(f"{model_id} advanced native parameters contain unknown keys {unknown}")

    # ``getParameters()`` returns a reference into the model. Keep the flat
    # model alive until the final constructor has copied the parameter object.
    flat_model = native_class(*flat_args)
    params = flat_model.getParameters()
    for key, value in zip(flat_keys, flat_args):
        _set_native_path(params, advanced_schema["flatMapping"][key], value)

    explicit_names: set[str] = set()
    for row in allowed_maps.values():
        map_value = advanced.get(row["key"])
        if isinstance(map_value, dict) and isinstance(map_value.get("byMaterial"), dict):
            explicit_names.update(str(name) for name in map_value["byMaterial"])
    names = sorted({str(name) for name in material_ids} | explicit_names)
    defaults = _read_native_values(params, advanced_schema, material_handle, names)

    for name, value in advanced.items():
        if name in allowed_blocks:
            if not isinstance(value, dict):
                raise RuntimeError(f"{model_id} advanced.{name} must be a JSON object")
            fields = {field["key"]: field for field in allowed_blocks[name]["fields"]}
            if unknown := sorted(set(value) - set(fields)):
                raise RuntimeError(f"{model_id} advanced.{name} contains unknown keys {unknown}")
            native = getattr(params, allowed_blocks[name]["native"])
            for key, entry in value.items():
                if fields[key]["kind"] == "boolean":
                    if type(entry) is not bool:
                        raise RuntimeError(f"{model_id} advanced.{name}.{key} must be a boolean")
                    setattr(native, key, entry)
                else:
                    if isinstance(entry, bool):
                        raise RuntimeError(f"{model_id} advanced.{name}.{key} must be a number")
                    setattr(native, key, float(entry))
        elif name in allowed_scalars:
            field = allowed_scalars[name]
            if field["kind"] == "boolean":
                if type(value) is not bool:
                    raise RuntimeError(f"{model_id} advanced.{name} must be a boolean")
                setattr(params, field["key"], value)
            else:
                if isinstance(value, bool):
                    raise RuntimeError(f"{model_id} advanced.{name} must be a number")
                setattr(params, field["key"], float(value))
        else:
            row = allowed_maps[name]
            if not isinstance(value, dict) or not value:
                raise RuntimeError(f"{model_id} advanced.{name} must be a nonempty JSON object")
            if unknown := sorted(set(value) - {"default", "byMaterial"}):
                raise RuntimeError(f"{model_id} advanced.{name} contains unknown keys {unknown}")
            native = getattr(params, row["native"])
            if "default" in value:
                native.setDefault(float(value["default"]))
            rows = value.get("byMaterial", {})
            if not isinstance(rows, dict):
                raise RuntimeError(f"{model_id} advanced.{name}.byMaterial must be a JSON object")
            for material_name, entry in rows.items():
                native.set(material_handle(str(material_name)), float(entry))

    effective = _read_native_values(params, advanced_schema, material_handle, names)
    model = native_class(params)
    metadata = {
        "parameterClass": advanced_schema["parameterClass"],
        "constructor": spec["nativeClass"],
        "constructorOverload": "parameter-object; flat values applied first, then advanced overrides",
        "flatMapping": dict(advanced_schema["flatMapping"]),
        "requested": advanced,
        "defaults": defaults,
        "effective": effective,
        "defaultsSource": "installed flat overload parameters before advanced overrides",
    }
    return model, metadata


def _coverage_parameters(ps: Any, parameters: dict[str, Any]) -> tuple[Any, dict[str, Any] | None]:
    """Build the native plasma ``CoverageParameters`` for one step.

    The worker has always pinned tolerance=1e-3 while the native struct default
    is 0.0. Omitting ``coverageTolerance`` must keep that historical value and
    add no new metadata; only an explicit request records the readback.
    """
    coverage = ps.CoverageParameters()
    if "coverageTolerance" not in parameters:
        coverage.tolerance = 1e-3
        return coverage, None
    if messages := coverage_tolerance_issues(parameters["coverageTolerance"]):
        raise RuntimeError(f"CoverageParameters.tolerance {'; '.join(messages)}")
    coverage.tolerance = float(parameters["coverageTolerance"])
    native_readback = {str(key): [float(value) for value in values]
                       for key, values in coverage.toMetaData().items()}
    return coverage, {
        "nativeClass": "CoverageParameters",
        "nativeMember": "tolerance",
        "requested": float(parameters["coverageTolerance"]),
        "effective": float(coverage.tolerance),
        "nativeDefault": 0.0,
        "workbenchHistoricalDefault": 1e-3,
        "source": "explicit",
        "nativeReadback": native_readback,
        "coverageInitIterations": (
            "the native CoverageParameters.maxIterations is left at its default; when tolerance==0 the "
            "installed FluxProcessStrategy replaces it with 10 initialization iterations, while a positive "
            "tolerance iterates until every coverage delta metric is <= tolerance"),
    }


def build_specialized_model(ps: Any, domain: Any, project: dict[str, Any], step: dict[str, Any],
                            material_handle: Any, material_ids: dict[str, int],
                            *, flux_engine: str | None = None) -> tuple[Any, list[Any], dict[str, Any], float]:
    """Build an installed native model and its native process parameters.

    The model ID is an application allowlist key; submitted JSON never selects
    an arbitrary Python attribute or constructor.

    ``flux_engine`` is the engine the caller is about to pin on ``ps.Process``.
    It is required for 2D ``faradayCage``: the shared CPU_DISK gate rejects an
    omitted or non-CPU engine before any native object is constructed, because
    the AUTO/GPU 2D kernel hangs for part of the cageAngle/tiltAngle matrix.
    """
    model_id = step["nativeModel"]
    spec = model_spec(model_id)
    if spec is None:
        raise RuntimeError(f"unavailable ViennaPS 4.6.2 model {model_id!r}")
    p = step["nativeParameters"]
    # Worker-side preflight: replay/direct worker inputs reach this function
    # without backend.validation. Keep the same safety rules here so an
    # invalid optional value can never reach a native setter/constructor.
    if model_id == "faradayCage" and int(project["dimension"]) == 2:
        gate_message = faraday_cage_2d_engine_error(flux_engine)
        if gate_message is not None:
            raise RuntimeError(f"FaradayCageEtching rejected before the native call: {gate_message}")
    if model_id in IBE_ENERGY_MODELS:
        # Same rule as backend.validation: thresholdEnergy >= meanEnergy makes
        # the native IBE yield change sign, so reject before IBEParameters is
        # constructed (a hand-edited/replayed worker input reaches this point
        # without validation).
        for suffix, message in ibe_energy_issues(p):
            raise RuntimeError(f"{spec['nativeClass']} rejected before the native call: {suffix}: {message}")
    if "coverageTolerance" in p:
        if model_id not in PLASMA_COVERAGE_MODELS:
            raise RuntimeError(f"{model_id} does not accept nativeParameters.coverageTolerance")
        if messages := coverage_tolerance_issues(p["coverageTolerance"]):
            raise RuntimeError(f"{model_id} coverageTolerance {'; '.join(messages)}")
    if "nvFactors" in p:
        if model_id != "selectiveEpitaxy":
            raise RuntimeError(f"{model_id} does not accept nativeParameters.nvFactors")
        if messages := nv_factors_issues(p["nvFactors"]):
            raise RuntimeError(f"selectiveEpitaxy nvFactors {'; '.join(messages)}")
    if model_id == "geometricTrenchDeposition":
        if problems := geometric_trench_issues(project, step):
            detail = "; ".join(f"{path}: {message}" for path, message in problems)
            raise RuntimeError(f"GeometricTrenchDeposition rejected before the native call: {detail}")
    extra: list[Any] = []
    duration = float(step["duration"]) if step.get("duration") is not None else float(p["timeHours"]) * 3600.0
    resolved: dict[str, Any] = {"nativeClass": spec["nativeClass"], "nativeModel": model_id,
                                "operation": step["operation"], "nativeParameters": p,
                                "durationSeconds": duration, "cpuThreads": int(project["solver"]["cpuThreads"]),
                                "nativeProcessEngine": "CPU_DISK pinned by the worker; no silent fallback"}

    if model_id == "sf6o2":
        model, advanced_metadata = _build_plasma_model(
            ps, model_id, spec, p,
            ("ionFlux", "etchantFlux", "oxygenFlux", "meanIonEnergy", "sigmaIonEnergy",
             "ionExponent", "oxySputterYield", "etchStopDepth"),
            material_handle, material_ids)
        if advanced_metadata is not None:
            resolved["nativeAdvancedParameters"] = advanced_metadata
        coverage, coverage_metadata = _coverage_parameters(ps, p)
        if coverage_metadata is not None:
            resolved["nativeCoverageParameters"] = coverage_metadata
        extra.append(coverage)
    elif model_id == "cf4o2":
        model, advanced_metadata = _build_plasma_model(
            ps, model_id, spec, p,
            ("ionFlux", "etchantFlux", "oxygenFlux", "polymerFlux", "meanIonEnergy",
             "sigmaIonEnergy", "ionExponent", "oxySputterYield", "polySputterYield", "etchStopDepth"),
            material_handle, material_ids)
        if advanced_metadata is not None:
            resolved["nativeAdvancedParameters"] = advanced_metadata
        coverage, coverage_metadata = _coverage_parameters(ps, p)
        if coverage_metadata is not None:
            resolved["nativeCoverageParameters"] = coverage_metadata
        extra.append(coverage)
    elif model_id == "hbro2":
        model, advanced_metadata = _build_plasma_model(
            ps, model_id, spec, p,
            ("ionFlux", "etchantFlux", "oxygenFlux", "meanIonEnergy", "sigmaIonEnergy",
             "ionExponent", "oxySputterYield", "etchStopDepth"),
            material_handle, material_ids)
        if advanced_metadata is not None:
            resolved["nativeAdvancedParameters"] = advanced_metadata
        coverage, coverage_metadata = _coverage_parameters(ps, p)
        if coverage_metadata is not None:
            resolved["nativeCoverageParameters"] = coverage_metadata
        extra.append(coverage)
    elif model_id == "sf6c4f8":
        model, advanced_metadata = _build_plasma_model(
            ps, model_id, spec, p,
            ("ionFlux", "etchantFlux", "meanEnergy", "sigmaEnergy", "ionExponent", "etchStopDepth"),
            material_handle, material_ids)
        if advanced_metadata is not None:
            resolved["nativeAdvancedParameters"] = advanced_metadata
        coverage, coverage_metadata = _coverage_parameters(ps, p)
        if coverage_metadata is not None:
            resolved["nativeCoverageParameters"] = coverage_metadata
        extra.append(coverage)
    elif model_id == "fluorocarbon":
        parameters = ps.FluorocarbonParameters()
        for key in ("ionFlux", "etchantFlux", "polyFlux", "k_ie", "k_ev", "delta_p", "temperature", "etchStopDepth"):
            setattr(parameters, key, float(p[key]))
        ions = parameters.Ions
        for key in ("meanEnergy", "sigmaEnergy", "exponent", "n_l", "inflectAngle", "minAngle"):
            setattr(ions, key, float(p[key]))
        for name, values in sorted(p["materialParametersByMaterial"].items()):
            material = ps.Material.Polymer if name == "Polymer" else material_handle(name)
            row = ps.FluorocarbonMaterialParameters()
            row.id = material
            for key, value in values.items():
                setattr(row, key, float(value))
            parameters.addMaterial(row)
        model = ps.FluorocarbonEtching(parameters)
    elif model_id == "ionBeam":
        parameters = ps.IBEParameters()
        for key in ("meanEnergy", "sigmaEnergy", "thresholdEnergy", "thetaRMin", "thetaRMax",
                    "minAngle", "inflectAngle", "exponent", "n_l", "tiltAngle", "planeWaferRate",
                    "redepositionRate", "redepositionThreshold"):
            setattr(parameters, key, float(p[key]))
        for index, key in enumerate(("cos4YieldA1", "cos4YieldA2", "cos4YieldA3", "cos4YieldA4"), start=1):
            setattr(parameters.cos4Yield, f"a{index}", float(p[key]))
        for name, value in sorted(p["materialPlaneWaferRateByMaterial"].items()):
            parameters.materialPlaneWaferRate.set(material_handle(name), float(value))
        masks = [material_handle(name) for name in sorted(_mask_material_names(project))]
        model = ps.IonBeamEtching(parameters, masks)
    elif model_id == "faradayCage":
        # FaradayCageParameters wraps an IBEParameters struct and adds the cage
        # tilt. This must stay a real FaradayCageEtching construction: silently
        # substituting IonBeamEtching would drop the two-direction cage source.
        parameters = ps.FaradayCageParameters()
        for key in ("meanEnergy", "sigmaEnergy", "thresholdEnergy", "thetaRMin", "thetaRMax",
                    "minAngle", "inflectAngle", "exponent", "n_l", "tiltAngle", "planeWaferRate",
                    "redepositionRate", "redepositionThreshold"):
            setattr(parameters.ibeParams, key, float(p[key]))
        for index, key in enumerate(("cos4YieldA1", "cos4YieldA2", "cos4YieldA3", "cos4YieldA4"), start=1):
            setattr(parameters.ibeParams.cos4Yield, f"a{index}", float(p[key]))
        for name, value in sorted(p["materialPlaneWaferRateByMaterial"].items()):
            parameters.ibeParams.materialPlaneWaferRate.set(material_handle(name), float(value))
        parameters.cageAngle = float(p["cageAngle"])
        mask_names = sorted(_mask_material_names(project))
        masks = [material_handle(name) for name in mask_names]
        model = ps.FaradayCageEtching(parameters, masks)
        resolved["nativeParameterContainer"] = {
            "nativeClass": "FaradayCageParameters",
            "ibeParametersClass": "IBEParameters",
            "dimension": int(project["dimension"]),
            "fluxEngine": flux_engine,
            "fluxEngineGate": ("CPU_DISK only in 2D" if int(project["dimension"]) == 2
                               else "not restricted; 3D behaviour is unchanged"),
            "cageAngle": float(parameters.cageAngle),
            "ibeParameters": {key: float(getattr(parameters.ibeParams, key)) for key in (
                "meanEnergy", "sigmaEnergy", "thresholdEnergy", "thetaRMin", "thetaRMax", "minAngle",
                "inflectAngle", "exponent", "n_l", "tiltAngle", "planeWaferRate", "redepositionRate",
                "redepositionThreshold")},
            "cos4Yield": {f"a{index}": float(getattr(parameters.ibeParams.cos4Yield, f"a{index}"))
                          for index in range(1, 5)},
            "maskMaterials": mask_names,
            "materialPlaneWaferRateByMaterial": {name: float(value)
                                                 for name, value in sorted(p["materialPlaneWaferRateByMaterial"].items())},
            "maskMaterialPlaneWaferRateForcedZero": True,
            "boundaryRequirement": "ViennaPS 4.6.2 FaradayCageEtching uses a periodic source and warns unless both lateral boundaries are periodic",
        }
    elif model_id == "wetEtch":
        material_rates = [(material_handle(name), float(value)) for name, value in sorted(p["materialRates"].items())]
        if p["useCrystallineRates"]:
            model = ps.WetEtching([float(v) for v in p["direction100"]], [float(v) for v in p["direction010"]],
                                  float(p["rate100"]), float(p["rate110"]), float(p["rate111"]),
                                  float(p["rate311"]), material_rates)
        else:
            model = ps.WetEtching(material_rates)
    elif model_id == "teosDeposition":
        model = ps.TEOSDeposition(*(float(p[key]) for key in (
            "stickingProbabilityP1", "rateP1", "orderP1", "stickingProbabilityP2", "rateP2", "orderP2")))
    elif model_id == "teospecvd":
        model = ps.TEOSPECVD(*(float(p[key]) for key in (
            "stickingProbabilityRadical", "depositionRateRadical", "depositionRateIon", "exponentIon",
            "stickingProbabilityIon", "reactionOrderRadical", "reactionOrderIon", "minAngleIon")))
    elif model_id == "ald":
        parameters = ps.SingleParticleALDParams()
        for key in ("stickingProbability", "gasMeanFreePath", "growthPerCycle", "incomingFlux",
                    "evaporationFlux", "s0", "coverageDiffusionCoefficient"):
            setattr(parameters, key, float(p[key]))
        model = ps.SingleParticleALD(parameters)
        atomic = ps.AtomicLayerProcessParameters()
        for key in ("pulseTime", "purgePulseTime", "coverageTimeStep", "purgeTimeStep"):
            setattr(atomic, key, float(p[key]))
        atomic.numCycles = int(p["numCycles"])
        extra.append(atomic)
        surface_diffusion = step.get("surfaceDiffusion")
        if surface_diffusion is not None and surface_diffusion.get("enabled", True):
            # Same rule as API validation: a hand-edited worker input must not
            # reach the known hanging 4.7.0 native stencil. Fail before any
            # SurfaceDiffusionParameters setter is constructed.
            gate_message = ald_surface_diffusion_error()
            if gate_message is not None:
                raise RuntimeError(gate_message)
            coefficient = float(p["coverageDiffusionCoefficient"])
            if not math.isfinite(coefficient) or coefficient <= 0.0:
                # ViennaPS 4.6.2 sets the ALP surface-diffusion flag only from
                # a positive coverage diffusion coefficient. Without it the
                # native solver is never built, so the object would be inert.
                raise RuntimeError("surfaceDiffusion requires coverageDiffusionCoefficient > 0")
            native = ps.SurfaceDiffusionParameters()
            effective = {
                "kNeighbors": int(surface_diffusion.get("kNeighbors", native.kNeighbors)),
                "normalCutoff": float(surface_diffusion.get("normalCutoff", native.normalCutoff)),
                "normalizeByLocalScale": bool(surface_diffusion.get(
                    "normalizeByLocalScale", native.normalizeByLocalScale)),
                "radius": float(surface_diffusion.get("radius", native.radius)),
                "sigmaNormal": float(surface_diffusion.get("sigmaNormal", native.sigmaNormal)),
                "stabilityFactor": float(surface_diffusion.get(
                    "stabilityFactor", native.stabilityFactor)),
                "symmetrizeWeights": bool(surface_diffusion.get(
                    "symmetrizeWeights", native.symmetrizeWeights)),
            }
            for key, value in effective.items():
                setattr(native, key, value)
            radius_limit = min(2.0, 8.0 * float(project["domain"]["gridDelta"]))
            if effective["radius"] > radius_limit:
                raise RuntimeError(
                    "surfaceDiffusion.radius must not exceed 8 grid intervals "
                    f"({radius_limit:g} um); radius=0 with kNeighbors keeps the stencil bounded")
            extra.append(native)
            # Read the installed binding back instead of trusting the request.
            recorded = {
                "nativeClass": "SurfaceDiffusionParameters",
                "requested": surface_diffusion,
                "effective": effective,
                "nativeMetaData": native.toMetaData(),
                "nativeMetaDataString": native.toMetaDataString(),
                "coverageDiffusionCoefficient": coefficient,
                "appliesTo": "ALD coverage scalar on the disk mesh; not the level-set geometry",
                "nativeFlagSource": "SingleParticleALDParams.coverageDiffusionCoefficient > 0",
                "nativeInsertionPoint": "Process.setParameters(SurfaceDiffusionParameters)",
            }
            for key in ("kNeighbors", "normalCutoff", "normalizeByLocalScale", "radius",
                        "sigmaNormal", "stabilityFactor", "symmetrizeWeights"):
                if getattr(native, key) != effective[key]:
                    raise RuntimeError(f"installed SurfaceDiffusionParameters.{key} did not keep its value")
            resolved["surfaceDiffusion"] = recorded
    elif model_id == "oxidation":
        model = ps.Oxidation()
        # viennals 5.8.5 spelled these enum members ``Dry``/``Wet``; 5.10.0
        # renamed them to ``DRY``/``WET``. Accept both so the worker keeps
        # working on either installed runtime.
        oxidant = getattr(ps.OxidantType, p["oxidant"], None)
        if oxidant is None:
            oxidant = getattr(ps.OxidantType, str(p["oxidant"]).upper())
        model.setOxidant(oxidant)
        model.setOrientation(getattr(ps.SiliconOrientation, p["orientation"]))
        model.setTemperature(float(p["temperatureC"]))
        model.setPressure(float(p["pressureAtm"]))
        model.setTime(float(p["timeHours"]))
        model.setTimeStep(float(p["timeStepHours"]))
        model.setTransferCoefficient(float(p["transferCoefficientUmHr"]))
        model.setInitialOxideThickness(float(p["initialOxideThicknessUm"]))
        model.setReactionActivationVolume(float(p["reactionActivationVolumeM3"]))
        model.setDiffusionActivationVolume(float(p["diffusionActivationVolumeM3"]))
        model.setSiliconMaterial(material_handle(p["siliconMaterial"]))
        model.setOxideMaterial(material_handle(p["oxideMaterial"]))
        if p["maskMaterial"]:
            model.setMaskMaterial(material_handle(p["maskMaterial"]))
        resolved["nativeParameters"] = {**p, "processDurationDerivation": "timeHours * 3600 seconds"}
        advanced = step.get("advancedOxidation")
        if advanced is not None:
            # This is the only place the advanced object reaches the native
            # model. The schema already proved the values are finite and
            # in-range; repeat the type/domain checks here so a request that
            # bypassed Pydantic (e.g. a hand-edited normalized project) cannot
            # reach a native setter with a bad value.
            spec = oxidation_advanced_spec()
            fields = {field["key"]: field for field in spec["fields"]}
            if not isinstance(advanced, dict):
                raise RuntimeError("advancedOxidation must be a JSON object")
            if unknown := sorted(set(advanced) - set(fields)):
                raise RuntimeError(f"advancedOxidation contains unknown keys {unknown}")
            effective: dict[str, float] = {}
            for key, field in fields.items():
                value = advanced.get(key, field["default"])
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise RuntimeError(f"advancedOxidation.{key} must be a finite number")
                number = float(value)
                if not math.isfinite(number):
                    raise RuntimeError(f"advancedOxidation.{key} must be a finite number")
                if not float(field["minimum"]) <= number <= float(field["maximum"]):
                    raise RuntimeError(
                        f"advancedOxidation.{key} must be between "
                        f"{field['minimum']} and {field['maximum']}")
                effective[key] = number
            if effective["couplingTolerance"] < effective["mechanicsTolerance"]:
                raise RuntimeError(
                    "advancedOxidation.couplingTolerance must be at least mechanicsTolerance")
            for restricted in ("simplePressureRelaxation", "mechanicsTolerance",
                               "couplingTolerance"):
                if (effective[restricted] != fields[restricted]["default"]
                        and not oxidation_advanced_supported(restricted)):
                    raise RuntimeError(
                        f"ViennaPS {engine_version()} does not honour "
                        f"advancedOxidation.{restricted}; use the ViennaPS 4.6.2 runtime "
                        f"or omit this override")
            model.setCFLFactor(effective["cflFactor"])
            model.setMechanicsTolerance(effective["mechanicsTolerance"])
            model.setCouplingTolerance(effective["couplingTolerance"])
            # 4.7.0 removed setSimplePressureRelaxation (pressure/stokes
            # controls moved into OxidationParameters). Keep sending it when
            # the binding has it; otherwise record that the knob is inert.
            pressure_relaxation_applied = hasattr(model, "setSimplePressureRelaxation")
            if pressure_relaxation_applied:
                model.setSimplePressureRelaxation(effective["simplePressureRelaxation"])
            # ``Oxidation`` has no getters for these solver controls. The only
            # native readback available on the installed binding is the
            # Deal-Grove planar estimate, which also re-reads the orientation.
            readback = {
                "orientation": str(p["orientation"]),
                "planarOxideThicknessUm": float(model.estimatePlanarOxideThickness(
                    float(p["initialOxideThicknessUm"]))),
                "solverControls": "no native getters in the installed viennaps; requested == effective after schema validation",
                "nativeGetters": "not exposed",
                "pressureRelaxationSetter": ("applied" if pressure_relaxation_applied
                                             else "removed by the installed viennaps (4.7.0)"),
            }
            resolved["advancedOxidation"] = {
                "schemaVersion": 1,
                "location": spec["location"],
                "nativeClass": spec["nativeClass"],
                # The worker only sees the normalized project; Pydantic has
                # already materialised every default, so the raw submitted key
                # subset is not recoverable without changing the request hash
                # or inventing replay-only state. Name the value honestly.
                "normalizedInput": dict(advanced),
                "inputSource": "normalized project; schema defaults are materialised before the worker",
                "defaults": {key: field["default"] for key, field in fields.items()},
                "effective": effective,
                "nativeSetters": {key: field["nativeSetter"] for key, field in fields.items()},
                "nativeReadback": readback,
                "defaultsSource": spec["defaultsSource"],
                "runtimeGate": spec.get("runtimeGate", {}),
            }
    elif model_id == "oxideRegrowth":
        surface = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
        vertical_axis = 1 if int(project["dimension"]) == 2 else 2
        top = max(float(point[vertical_axis]) for point in surface.getNodes())
        padding = max(1.0, 5.0 * float(project["domain"]["gridDelta"]))
        cellset_height = top + padding
        domain.generateCellSet(cellset_height, ps.Material.GAS, True)
        cell_set = domain.getCellSet()
        if cell_set is None:
            raise RuntimeError("ViennaPS Domain.generateCellSet returned no DenseCellSet")
        cell_set.addScalarData("byproductSum", 0.0)
        cell_set.buildNeighborhood()
        model = ps.OxideRegrowth(*(float(p[key]) for key in (
            "nitrideEtchRate", "oxideEtchRate", "redepositionRate", "redepositionThreshold",
            "redepositionTimeInt", "diffusionCoefficient", "sinkStrength", "scallopVelocity",
            "centerVelocity", "topHeight", "centerWidth", "stabilityFactor")))
        resolved["derivedCellSet"] = {"nativeType": "DenseCellSet", "material": "GAS", "height": cellset_height,
                                      "verticalAxis": vertical_axis, "initialField": "byproductSum=0",
                                      "neighborhoodBuilt": True}
    elif model_id == "selectiveEpitaxy":
        # ViennaPS 4.6.2 requires the native epitaxy enum identity in this
        # overload; registry handles do not satisfy its top-material check.
        rates = [(getattr(ps.Material, name), float(value)) for name, value in sorted(p["materialRates"].items())]
        requested_nv = p.get("nvFactors")
        if requested_nv is None:
            model = ps.SelectiveEpitaxy(rates, float(p["rate111"]), float(p["rate100"]))
        else:
            # The nvFactors overload is a separate native constructor: it leaves
            # materialRates_ empty, so every epitaxy rate must be applied through
            # setMaterialRate (the velocity field holds that map by reference).
            # Do not replace this with the materialRates overload: that would
            # silently drop the requested factors.
            factors = [float(value) for value in requested_nv]
            model = ps.SelectiveEpitaxy(factors, float(p["rate111"]), float(p["rate100"]))
            for material, value in rates:
                model.setMaterialRate(material, value)
            resolved["nativeNvFactors"] = {
                "nativeClass": "SelectiveEpitaxy",
                "nativeModule": f"viennaps.d{int(project['dimension'])}",
                "constructorOverload": ("viennaps.d%d.SelectiveEpitaxy(nvFactors, rate111, rate100)"
                                        % int(project["dimension"])),
                "requested": factors,
                "effective": factors,
                "nativeDefaultByDimension": {"2": [0.5, 1.0, 0.0], "3": [0.5, 0.0, 1.0]},
                "materialRatesAppliedAfterConstruction": {
                    name: float(value) for name, value in sorted(p["materialRates"].items())},
                "velocityFormula": ("max_i(|nvFactors[i]*normal[i]|) mapped from [1/sqrt(D), 1] "
                                    "to [rate111, rate100]; the result is clamped to <= 0 by the native model"),
                "nativeReadback": None,
                "nativeReadbackReason": ("the installed binding exposes no nvFactors getter; getVelocityField() "
                                         "returns an unregistered EpitaxyVelocityField that cannot be converted to Python"),
            }
    elif model_id == "geometricTrenchDeposition":
        # GeometricTrenchDeposition is a ViennaLS geometric-advection
        # distribution. It has no material table and no native mask; the worker
        # adds the new level set with Domain.duplicateTopLevelSet(depositMaterial)
        # before Process.apply(), so that top level set is what grows.
        argument_keys = ("trenchWidth", "trenchDepth", "depositionRate", "bottomMed", "a", "b", "n")
        arguments = {key: float(p[key]) for key in argument_keys}
        model = ps.GeometricTrenchDeposition(*(arguments[key] for key in argument_keys))
        resolved["nativeGeometricAdvection"] = {
            "nativeClass": "GeometricTrenchDeposition",
            "constructorArguments": arguments,
            "depositMaterial": step.get("depositMaterial"),
            "addsNewLevelSetThrough": "Domain.duplicateTopLevelSet(depositMaterial) before Process.apply()",
            "nativeMask": None,
            "trenchWidthUsedByNativeKernel": False,
            "durationScalesThickness": False,
            "depthReference": "absolute z; radius = a*(1-|z|/trenchDepth)^n + b with the trench top assumed at z=0 and bottomMed at z=-trenchDepth",
            "nativeTiming": "one geometric advection per Process.apply(); the process duration does not change the deposited thickness",
        }
    elif model_id == "neutralTransport":
        parameters = ps.NeutralTransportParameters()
        for key in ("incomingFlux", "sourceDistributionPower", "etchFrontSticking", "zeroCoverageSticking",
                    "coverageTimeStep", "desorptionRate", "surfaceDiffusionCoefficient", "surfaceSiteDensity"):
            setattr(parameters, key, float(p[key]))
        parameters.useSteadyStateCoverage = bool(p["useSteadyStateCoverage"])
        parameters.etchFrontMaterial = material_handle(p["etchFrontMaterial"])
        parameters.fluxLabel = str(p["fluxLabel"])
        parameters.coverageLabel = str(p["coverageLabel"])
        model = ps.NeutralTransport(parameters)
    else:
        raise RuntimeError(f"no native constructor is implemented for {model_id!r}")

    resolved["materialIdsByName"] = {name: material_ids.get(name) for name in _referenced_materials(p)}
    resolved["nativeProcessParameters"] = [type(item).__name__ for item in extra]
    return model, extra, resolved, duration


def _referenced_materials(parameters: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for key in ("siliconMaterial", "oxideMaterial", "maskMaterial", "etchFrontMaterial"):
        value = parameters.get(key)
        if isinstance(value, str) and value:
            result.add(value)
    for key in ("materialRates", "materialPlaneWaferRateByMaterial", "materialParametersByMaterial"):
        value = parameters.get(key)
        if isinstance(value, dict):
            result.update(str(name) for name in value if name != "Polymer")
    return result


def _mask_material_names(project: dict[str, Any]) -> set[str]:
    geometry = project.get("geometry", {})
    if geometry.get("kind") == "gds":
        return {str(row["material"]) for row in geometry.get("layers", []) if row.get("mask")}
    names: set[str] = set()
    if geometry.get("kind") == "stack":
        names.update(str(row["material"]) for row in geometry.get("layers", []) if row.get("role") == "mask")
    elif geometry.get("kind") in {"trench", "hole", "fin"} and float(geometry.get("maskHeight", 0.0)) > 0:
        names.add(str(geometry.get("maskMaterial", "Mask")))
    return names
