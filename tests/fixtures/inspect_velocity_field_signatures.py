"""Inspect installed ViennaPS/ViennaLS hooks that might safely combine ion fluxes."""
from __future__ import annotations

import inspect
import json

import viennals
import viennaps


def describe(value):
    result = {"repr": repr(value), "doc": getattr(value, "__doc__", None),
              "textSignature": getattr(value, "__text_signature__", None)}
    try:
        result["signature"] = str(inspect.signature(value))
    except (TypeError, ValueError):
        result["signature"] = None
    return result


report = {"versions": {"viennaps": viennaps.__version__, "viennals": viennals.__version__},
          "velocityField": {name: describe(getattr(viennals.VelocityField, name))
                            for name in ("__init__", "getScalarVelocity", "getVectorVelocity", "getDissipationAlpha")},
          "dimensions": {}}
for dimension in (viennaps.d2, viennaps.d3):
    report["dimensions"][dimension.__name__] = {
        "setVelocityField": describe(dimension.ProcessModel.setVelocityField),
        "getVelocityField": describe(dimension.ProcessModel.getVelocityField),
        "multiParticleSetRateFunction": describe(dimension.MultiParticleProcess.setRateFunction),
        "processCalculateFlux": describe(dimension.Process.calculateFlux),
    }

print(json.dumps(report, ensure_ascii=False, indent=2))
