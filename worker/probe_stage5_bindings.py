"""Inventory installed ViennaPS/ViennaLS geometry and GDS bindings.

Run only via scripts/bounded_child.py; this script does not construct or mutate a domain.
"""
from __future__ import annotations

import json
import sys
from importlib.metadata import version
from pathlib import Path


TOKENS = ("geom", "fin", "box", "plane", "stack", "hole", "trench", "gds", "extrud", "slice", "cut", "poly", "circle", "cylinder", "sphere", "stencil", "mesh", "contour", "transform", "rotate", "translate", "domain", "material", "boundary")


def safe_getattr(value, name):
    try:
        return getattr(value, name)
    except BaseException as error:
        return {"probeError": f"{type(error).__name__}: {error}"}


def public_members(value):
    try:
        return [name for name in dir(value) if not name.startswith("_")]
    except BaseException as error:
        return {"probeError": f"{type(error).__name__}: {error}"}


def describe(value):
    if isinstance(value, dict) and "probeError" in value:
        return value
    names = public_members(value)
    if isinstance(names, dict):
        return names
    description = {
        "type": type(value).__name__,
        "doc": (getattr(value, "__doc__", "") or "")[:5000],
        "constructorDoc": (getattr(getattr(value, "__init__", None), "__doc__", "") or "")[:6000],
        "members": [],
        "callables": {},
    }
    for name in names:
        member = safe_getattr(value, name)
        if isinstance(member, dict) and "probeError" in member:
            description["members"].append({"name": name, **member})
            continue
        if callable(member):
            description["callables"][name] = (getattr(member, "__doc__", "") or "")[:3000]
        else:
            description["members"].append({"name": name, "value": str(member)[:300]})
    return description


def main() -> None:
    import viennaps as ps

    report = {
        "versions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
        "modules": {},
        "symbols": {},
        "targetCandidates": {},
    }
    for module_name in ("viennaps", "viennaps.d2", "viennaps.d3", "viennaps.ls"):
        module = ps
        try:
            for part in module_name.split(".")[1:]:
                module = getattr(module, part)
        except BaseException as error:
            report["modules"][module_name] = {"probeError": f"{type(error).__name__}: {error}"}
            continue
        names = public_members(module)
        report["modules"][module_name] = names
        if isinstance(names, list):
            for name in names:
                lowered = name.lower()
                if not any(token in lowered for token in TOKENS):
                    continue
                member = safe_getattr(module, name)
                report["symbols"][f"{module_name}.{name}"] = describe(member)
    for name in public_members(ps):
        if isinstance(name, dict):
            continue
        if any(token in name.lower() for token in TOKENS):
            report["targetCandidates"][name] = describe(safe_getattr(ps, name))
    output = Path(sys.argv[1]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "versions": report["versions"],
                      "topLevelGeometryCandidates": sorted(report["targetCandidates"]),
                      "moduleGeometrySymbols": sorted(report["symbols"])}))


if __name__ == "__main__":
    main()
