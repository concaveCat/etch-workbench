"""Read constructor and default parameter surfaces for ViennaPS specialized models."""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import viennaps as ps


MODEL_NAMES = (
    "SF6O2Etching", "CF4O2Etching", "HBrO2Etching", "FluorocarbonEtching",
    "SF6C4F8Etching", "FaradayCageEtching", "IonBeamEtching", "WetEtching",
    "TEOSDeposition", "TEOSPECVD", "SingleParticleALD", "Oxidation", "OxideRegrowth",
    "SelectiveEpitaxy", "NeutralTransport",
)
PARAMETER_NAMES = (
    "PlasmaEtchingParameters", "PlasmaEtchingParametersIons", "PlasmaEtchingParametersPassivation",
    "PlasmaEtchingParametersMask", "PlasmaEtchingParametersPolymer", "PlasmaEtchingParametersSubstrate",
    "CF4O2Parameters", "CF4O2ParametersIons", "CF4O2ParametersPassivation", "FluorocarbonParameters",
    "FluorocarbonParametersIons", "IBEParameters", "FaradayCageParameters", "NeutralTransportParameters",
    "SingleParticleALDParams", "OxidationParameters", "OxidationDeformationParameters",
    "IBEParametersCos4Yield", "CoverageParameters", "AtomicLayerProcessParameters", "MaterialValueMap",
    "FluorocarbonMaterialParameters", "DenseCellSet", "Domain", "Length", "Time", "LengthUnit", "TimeUnit",
)


def safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [safe(item) for item in value]
    if isinstance(value, dict):
        return {str(k): safe(v) for k, v in value.items()}
    return repr(value)[:400]


def describe_instance(value):
    result = {"type": f"{type(value).__module__}.{type(value).__name__}", "attributes": {}, "methods": {}}
    try:
        result["dict"] = safe(vars(value))
    except Exception:
        result["dict"] = None
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(value, name)
        except Exception as exc:
            result["attributes"][name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        if callable(attr):
            text = getattr(attr, "__doc__", None)
            if name.startswith(("set", "get", "add", "calculate", "apply", "run", "generate", "insert", "duplicate", "write", "build")):
                result["methods"][name] = inspect.cleandoc(text)[:5000] if text else None
        else:
            result["attributes"][name] = {"value": safe(attr), "type": type(attr).__name__}
    return result


def main(path):
    output = {"version": getattr(ps, "__version__", None), "models": {}, "parameters": {}}
    for name in MODEL_NAMES:
        model_class = getattr(ps, name, None)
        if model_class is None:
            output["models"][name] = {"available": False}
            continue
        constructor = getattr(model_class, "__init__", None)
        row = {"available": True, "class": str(model_class),
               "constructorDoc": inspect.cleandoc(getattr(constructor, "__doc__", "") or "")[:12000]}
        try:
            instance = model_class()
            row["defaultInstance"] = describe_instance(instance)
            getter = getattr(instance, "getParameters", None)
            if callable(getter):
                row["defaultParameters"] = describe_instance(getter())
        except Exception as exc:
            row["defaultConstructionError"] = f"{type(exc).__name__}: {exc}"
        output["models"][name] = row
    for name in PARAMETER_NAMES:
        parameter_class = getattr(ps, name, None)
        if parameter_class is None:
            output["parameters"][name] = {"available": False}
            continue
        constructor = getattr(parameter_class, "__init__", None)
        row = {"available": True, "constructorDoc": inspect.cleandoc(getattr(constructor, "__doc__", "") or "")[:12000]}
        row["classMethods"] = {}
        for method_name in dir(parameter_class):
            if method_name.startswith("_"):
                continue
            try:
                method = getattr(parameter_class, method_name)
            except Exception:
                continue
            if callable(method):
                method_doc = getattr(method, "__doc__", None)
                if method_doc and method_name in ("setUnit", "getUnit", "setDefault", "set"):
                    row["classMethods"][method_name] = inspect.cleandoc(method_doc)[:3000]
        try:
            row["defaultInstance"] = describe_instance(parameter_class())
        except Exception as exc:
            row["defaultConstructionError"] = f"{type(exc).__name__}: {exc}"
        output["parameters"][name] = row
    output["enumMembers"] = {}
    for name in ("LengthUnit", "TimeUnit", "OxidantType", "SiliconOrientation", "GpuMode", "FluxEngineType"):
        enum = getattr(ps, name, None)
        output["enumMembers"][name] = list(getattr(enum, "__members__", {}).keys()) if enum else None
    try:
        ps.setDimension(2)
        ps.Length.setUnit("um")
        ps.Time.setUnit("s")
        domain = ps.Domain(0.2, 2.0)
        ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
        output["domainInstance"] = describe_instance(domain)
        try:
            domain.generateCellSet(0.5, ps.Material.GAS, True)
            output["cellSetInstance"] = describe_instance(domain.getCellSet())
        except Exception as exc:
            output["cellSetReadError"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        output["domainProbeError"] = f"{type(exc).__name__}: {exc}"
    Path(path).write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: stage8_parameter_inventory.py OUTPUT.json")
    main(sys.argv[1])
