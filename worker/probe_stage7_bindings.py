"""Inspect ViennaPS MultiParticleProcess bindings inside bounded_child.py."""
from __future__ import annotations

import json
import sys
from importlib.metadata import version
from pathlib import Path


def describe(value):
    methods = sorted(name for name in dir(value) if not name.startswith("_") and callable(getattr(value, name, None)))
    return {
        "name": getattr(value, "__name__", type(value).__name__),
        "doc": getattr(value, "__doc__", "") or "",
        "constructorDoc": getattr(getattr(value, "__init__", None), "__doc__", "") or "",
        "members": sorted(name for name in dir(value) if not name.startswith("_") and not callable(getattr(value, name, None))),
        "callables": methods,
        "methodDocs": {name: getattr(getattr(value, name), "__doc__", "") or "" for name in methods},
    }


def main(path: str) -> None:
    import viennaps as ps

    report = {
        "versions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
        "dimensions": {},
        "shared": {},
    }
    for dimension in (2, 3):
        module = getattr(ps, f"d{dimension}")
        report["dimensions"][str(dimension)] = {
            "multiParticleProcess": describe(module.MultiParticleProcess),
            "process": describe(module.Process),
        }
    for name in ("Material", "MaterialValueMap", "RayTracingParameters", "FluxEngineType", "NormalizationType"):
        value = getattr(ps, name, None)
        if value is not None:
            report["shared"][name] = describe(value)
    report["enumValues"] = {
        name: {member: int(value) for member, value in getattr(getattr(ps, name), "__members__", {}).items()}
        for name in ("FluxEngineType", "NormalizationType", "Material")
        if getattr(ps, name, None) is not None and hasattr(getattr(ps, name), "__members__")
    }
    parameters = ps.RayTracingParameters()
    report["rayDefaults"] = {
        key: getattr(parameters, key)
        for key in ("raysPerPoint", "diskRadius", "maxBoundaryHits", "maxReflections", "minNodeDistanceFactor",
                    "smoothingNeighbors", "rngSeed", "useRandomSeeds", "ignoreFluxBoundaries", "normalizationType")
    }
    report["rayMetadata"] = parameters.toMetaData()
    Path(path).write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n")
    print(json.dumps({"versions": report["versions"], "dimensions": sorted(report["dimensions"]),
                      "multiParticleMethods": report["dimensions"]["2"]["multiParticleProcess"]["callables"]}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
