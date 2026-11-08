"""Inspect the installed ViennaPS 4.6.2 Python bindings inside bounded_child."""
from __future__ import annotations

import json
from importlib.metadata import version


def describe(value):
    callables = sorted(name for name in dir(value) if not name.startswith("_") and callable(getattr(value, name, None)))
    return {
        "name": getattr(value, "__name__", type(value).__name__),
        "doc": (getattr(value, "__doc__", "") or "")[:12000],
        "constructorDoc": (getattr(getattr(value, "__init__", None), "__doc__", "") or "")[:12000],
        "members": sorted(name for name in dir(value) if not name.startswith("_") and not callable(getattr(value, name, None))),
        "callables": callables,
        "methodDocs": {name: (getattr(getattr(value, name), "__doc__", "") or "")[:5000] for name in callables},
    }


def main():
    import viennaps as ps

    names = [
        "Process", "AdvectionParameters", "RayTracingParameters", "DirectionalProcess",
        "SingleParticleProcess", "IsotropicProcess", "Flux", "FluxEngineType",
        "Data", "DataType", "Domain", "Writer", "Reader", "OutputType",
        "MultiParticleProcess", "SF6O2Etching", "CF4O2Etching", "IonBeamEtching",
        "WetEtching", "TEOSDeposition", "TEOSPECVD", "SingleParticleALD",
        "Oxidation", "OxideRegrowth", "SelectiveEpitaxy", "NeutralTransport",
        "MakeFin", "MakeStack", "GDSGeometry", "GDSReader",
    ]
    report = {
        "versions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
        "bindings": {},
    }
    for name in names:
        obj = getattr(ps, name, None)
        if obj is not None:
            report["bindings"][name] = describe(obj)
    for name in ("d2", "d3", "ls"):
        module = getattr(ps, name, None)
        if module is not None:
            report["bindings"][name] = {"members": sorted(item for item in dir(module) if not item.startswith("_"))}
    for name in ("SpatialSchemeEnum", "TemporalSchemeEnum", "IntegrationSchemeEnum", "NormalizationType", "OutputType", "FileFormatEnum"):
        if name in dir(ps.ls):
            report["bindings"][f"ls.{name}"] = describe(getattr(ps.ls, name))
    advection = ps.AdvectionParameters()
    rays = ps.RayTracingParameters()
    report["parameterDefaults"] = {
        "advection": {name: str(getattr(advection, name)) for name in dir(advection) if not name.startswith("_") and not callable(getattr(advection, name, None))},
        "advectionMetadata": advection.toMetaData(),
        "rayTracing": {name: str(getattr(rays, name)) for name in dir(rays) if not name.startswith("_") and not callable(getattr(rays, name, None))},
        "rayTracingMetadata": rays.toMetaData(),
        "normalizationMembers": {name: int(value) for name, value in getattr(ps.NormalizationType, "__members__", {}).items()},
        "spatialMembers": {name: int(value) for name, value in getattr(ps.ls.SpatialSchemeEnum, "__members__", {}).items()},
        "temporalMembers": {name: int(value) for name, value in getattr(ps.ls.TemporalSchemeEnum, "__members__", {}).items()},
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
