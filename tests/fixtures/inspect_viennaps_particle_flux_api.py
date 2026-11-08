"""Read-only inventory of the installed particle/flux Python API."""
from __future__ import annotations

import importlib.metadata
import inspect
import json

import viennaps as ps


def main() -> None:
    selected = [name for name in dir(ps) if any(token in name.lower()
                for token in ("particle", "flux", "process", "velocity", "rate"))]
    detail = {}
    for name in selected:
        value = getattr(ps, name)
        if isinstance(value, type):
            detail[name] = {"doc": (getattr(value, "__doc__", "") or "")[:1200],
                            "methods": [method for method in dir(value) if not method.startswith("_")]}
    method_docs = {}
    for cls_name, method_names in {
        "Process": ("calculateFlux", "apply", "setFluxEngineType", "setParameters"),
        "MultiParticleProcess": ("addIonParticle", "addNeutralParticle", "setRateFunction"),
        "ProcessModel": ("getVelocityField", "setVelocityField"),
    }.items():
        cls = getattr(ps, cls_name, None)
        if cls is None:
            continue
        method_docs[cls_name] = {}
        for method_name in method_names:
            method = getattr(cls, method_name, None)
            if method is not None:
                try:
                    signature = str(inspect.signature(method))
                except (TypeError, ValueError):
                    signature = None
                method_docs[cls_name][method_name] = {"signature": signature,
                    "doc": (getattr(method, "__doc__", "") or "")[:2000]}
    nested_symbols = {}
    for parent_name in ("d2", "d3", "ls"):
        parent = getattr(ps, parent_name, None)
        if parent is not None:
            nested_symbols[parent_name] = [name for name in dir(parent) if any(token in name.lower()
                for token in ("flux", "particle", "process", "velocity", "rate"))]
    print(json.dumps({"versions": {"viennaps": importlib.metadata.version("viennaps"),
                                   "viennals": importlib.metadata.version("viennals")},
                      "symbols": selected, "classes": detail, "methodDocs": method_docs,
                      "nestedSymbols": nested_symbols}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
