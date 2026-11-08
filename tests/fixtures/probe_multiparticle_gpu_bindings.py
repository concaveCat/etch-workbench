"""Record installed ViennaPS MultiParticle class routes without changing packages."""
from __future__ import annotations

import importlib.metadata
import inspect
import json
from pathlib import Path

import viennaps as ps


def describe(value: object) -> dict:
    cls = value if inspect.isclass(value) else type(value)
    methods = {}
    for name in ("__init__", "addIonParticle", "addNeutralParticle", "setRateFunction",
                 "setFluxEngineType", "calculateFlux", "getVelocityField", "setVelocityField"):
        method = getattr(value, name, None)
        if method is None:
            continue
        try:
            signature = str(inspect.signature(method))
        except (TypeError, ValueError):
            signature = None
        methods[name] = {"doc": getattr(method, "__doc__", None),
                         "signature": signature}
    return {"repr": repr(value), "class": f"{cls.__module__}.{cls.__qualname__}",
            "mro": [f"{base.__module__}.{base.__qualname__}" for base in getattr(cls, "__mro__", ())],
            "methods": methods}


def main() -> None:
    routes = {}
    for label, module in (("root", ps), ("d2", ps.d2), ("d3", ps.d3),
                          ("d2.gpu", getattr(ps.d2, "gpu", None)),
                          ("d3.gpu", getattr(ps.d3, "gpu", None)),
                          ("root.gpu", getattr(ps, "gpu", None))):
        if module is None:
            routes[label] = {"available": False}
            continue
        routes[label] = {"available": True, "module": getattr(module, "__name__", None),
                         "MultiParticleProcess": describe(getattr(module, "MultiParticleProcess", None))
                         if hasattr(module, "MultiParticleProcess") else None,
                         "ProcessModelGPU": describe(getattr(module, "ProcessModelGPU", None))
                         if hasattr(module, "ProcessModelGPU") else None}
    output = {"versions": {"viennaps": importlib.metadata.version("viennaps"),
                           "viennals": importlib.metadata.version("viennals")},
              "binary": str(Path(ps._C.__file__).resolve()), "gpuAvailable": bool(ps.gpuAvailable()),
              "routes": routes}
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
