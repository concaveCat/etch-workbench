"""Check whether the installed bindings let Python replace a native velocity field."""
from __future__ import annotations

import json

import viennals
import viennaps


class PythonVelocityField(viennals.VelocityField):
    def getScalarVelocity(self, coordinate, material_id, normal, level_set_id):
        return 0.0


report = {"versions": {"viennaps": viennaps.__version__, "viennals": viennals.__version__},
          "dimensions": {}}
for dimension in (viennaps.d2, viennaps.d3):
    model = dimension.MultiParticleProcess()
    model.addIonParticle(sourcePower=1.0, meanEnergy=120.0, sigmaEnergy=15.0,
                         thresholdEnergy=10.0, label="probeIon")
    item = {"modelType": str(type(model)), "attempts": []}
    for label, factory in (("nativeGetter", model.getVelocityField),
                           ("plainViennaLSBase", viennals.VelocityField),
                           ("pythonViennaLSSubclass", PythonVelocityField)):
        try:
            field = factory()
            try:
                model.setVelocityField(field)
                item["attempts"].append({"input": label, "accepted": True,
                                         "fieldType": str(type(field))})
            except Exception as exc:
                item["attempts"].append({"input": label, "accepted": False,
                                         "fieldType": str(type(field)),
                                         "errorType": type(exc).__name__, "error": str(exc)})
        except Exception as exc:
            item["attempts"].append({"input": label, "constructed": False,
                                     "errorType": type(exc).__name__, "error": str(exc)})
    report["dimensions"][dimension.__name__] = item

print(json.dumps(report, ensure_ascii=False, indent=2))
