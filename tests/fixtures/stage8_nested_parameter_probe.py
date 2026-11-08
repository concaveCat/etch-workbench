"""Inspect nested 4.6.2 model parameter objects via bounded_child."""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import viennaps as ps


def simple(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)[:300]


def describe(value):
    result = {"type": f"{type(value).__module__}.{type(value).__name__}", "fields": {}, "methods": {}}
    for name in dir(value):
        if name.startswith("_"): continue
        try: item = getattr(value, name)
        except Exception as exc:
            result["fields"][name] = {"error": f"{type(exc).__name__}: {exc}"}; continue
        if callable(item):
            doc = getattr(item, "__doc__", None)
            result["methods"][name] = inspect.cleandoc(doc)[:4000] if doc else None
        else:
            result["fields"][name] = simple(item)
    return result


def main(path: Path) -> None:
    ps.Length.setUnit("um"); ps.Time.setUnit("s")
    ibe = ps.IBEParameters()
    fc = ps.FluorocarbonParameters()
    output = {
        "version": getattr(ps, "__version__", None),
        "IBEParameters.cos4Yield": describe(ibe.cos4Yield),
        "MaterialValueMap": describe(ibe.materialPlaneWaferRate),
        "FluorocarbonParameters.Ions": describe(fc.Ions),
        "FluorocarbonMaterialParameters": describe(ps.FluorocarbonMaterialParameters()),
        "CF4O2Parameters": describe(ps.CF4O2Parameters()),
        "CF4O2ParametersIons": describe(ps.CF4O2ParametersIons()),
        "CF4O2ParametersPassivation": describe(ps.CF4O2ParametersPassivation()),
        "PlasmaEtchingParameters": describe(ps.PlasmaEtchingParameters()),
    }
    path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2: raise SystemExit("usage: stage8_nested_parameter_probe.py OUTPUT.json")
    main(Path(sys.argv[1]))
