"""Bounded probe: which advancedOxidation setters still change the 2D surface.

Replicates the Workbench oxidation configuration (2D, Si + SiO2 stack, dry,
1000 C, 0.166 h, 0.03 h steps) and compares a surface hash with and without
one setter at a time. Run under both the 4.6.2 and 4.7.0 runtimes.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
(ROOT / "var" / "probes").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts" / "migration-probes"))
from common import write  # noqa: E402

ps.setDimension(2)
ps.Logger.setLogLevel(ps.LogLevel.ERROR)
ps.setNumThreads(1)


def hash_surface(domain) -> tuple[str, int]:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False,
                                 minNodeDistanceFactor=0.01)
    points = [[round(float(v), 9) for v in point] for point in mesh.getNodes()]
    payload = json.dumps(points, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest(), len(points)


def run(setter: str | None, value: float, *, delta: float = 0.01,
        block: dict[str, float] | None = None) -> dict:
    domain = ps.Domain(delta, 1.0, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(domain, 0.0, ps.Material.Si).apply()
    ps.MakePlane(domain, max(0.05, 3.0 * delta), ps.Material.SiO2,
                 addToExisting=True).apply()
    model = ps.Oxidation()
    oxidant = getattr(ps.OxidantType, "Dry", None)
    if oxidant is None:
        oxidant = getattr(ps.OxidantType, "DRY")
    model.setOxidant(oxidant)
    model.setOrientation(ps.SiliconOrientation.Si100)
    model.setTemperature(1000.0)
    model.setPressure(1.0)
    model.setTime(0.166)
    model.setTimeStep(0.03)
    model.setTransferCoefficient(0.1)
    model.setInitialOxideThickness(0.0)
    model.setReactionActivationVolume(0.0)
    model.setDiffusionActivationVolume(0.0)
    model.setSiliconMaterial(ps.Material.Si)
    model.setOxideMaterial(ps.Material.SiO2)
    if block is not None:
        for name, number in block.items():
            native = {"cflFactor": "setCFLFactor"}.get(
                name, "set" + name[0].upper() + name[1:])
            getattr(model, native)(number)
    if setter is not None:
        getattr(model, setter)(value)
    process = ps.Process(domain, model, 0.166 * 3600.0)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = 0.25
    process.setParameters(advection)
    process.apply()
    digest, count = hash_surface(domain)
    return {"hash": digest, "points": count}


cases = [
    ("cflFactor", 0.05, 0.01),
    ("mechanicsTolerance", 0.05, 0.01),
    ("couplingTolerance", 0.2, 0.02),
    ("simplePressureRelaxation", 0.1, 0.01),
]
out = {
    "viennaps": getattr(ps, "__version__", ps.version),
    "viennals": ps.ls.version,
    "boundSetters": sorted(name for name in dir(ps.Oxidation)
                           if name.startswith("set") and not name.startswith("_")),
    "cases": {},
}
base = run(None, 0.0)
out["baseline"] = base
for setter, value, delta in cases:
    native_setter = {"cflFactor": "setCFLFactor"}.get(
        setter, "set" + setter[0].upper() + setter[1:])
    if not hasattr(ps.Oxidation, native_setter):
        out["cases"][setter] = {"bound": False, "changed": False}
        continue
    result = run(native_setter, value, delta=delta)
    out["cases"][setter] = {
        "bound": True,
        "value": value,
        "delta": delta,
        "hash": result["hash"],
        "points": result["points"],
        "changed": result["hash"] != base["hash"],
    }
# The Workbench materializes the block defaults before the worker. Compare
# that exact shape too: a baseline with the 4.6.2 defaults applied, then one
# field changed. simplePressureRelaxation is only included when bound.
DEFAULTS = {"cflFactor": 0.499, "mechanicsTolerance": 0.01,
            "couplingTolerance": 0.05}
thresholds = [("cflFactor", 0.05, 0.01), ("mechanicsTolerance", 0.05, 0.01),
              ("couplingTolerance", 0.2, 0.02)]
out["workbenchBlock"] = {"defaults": DEFAULTS, "baselines": {}, "cases": {}}
for setter, value, delta in thresholds:
    changed = dict(DEFAULTS)
    changed[setter] = value
    base = out["workbenchBlock"]["baselines"].setdefault(
        str(delta), run(None, 0.0, delta=delta, block=DEFAULTS))
    result = run(None, 0.0, delta=delta, block=changed)
    out["workbenchBlock"]["cases"][setter] = {
        "value": value, "delta": delta, "hash": result["hash"],
        "changed": result["hash"] != base["hash"]}
if "setSimplePressureRelaxation" in out["boundSetters"]:
    changed = dict(DEFAULTS, simplePressureRelaxation=0.1)
    result = run(None, 0.0, block=changed)
    base = out["workbenchBlock"]["baselines"].setdefault(
        "0.01", run(None, 0.0, block=DEFAULTS))
    out["workbenchBlock"]["cases"]["simplePressureRelaxation"] = {
        "value": 0.1, "delta": 0.01, "hash": result["hash"],
        "changed": result["hash"] != base["hash"]}

write(str(ROOT / "var" / "probes" / "oxidation_setters.json"), out)
print(json.dumps({"version": out["viennaps"],
                  "cases": {k: {"bound": v["bound"], "changed": v.get("changed")}
                            for k, v in out["cases"].items()},
                  "workbenchBlock": {k: v.get("changed") for k, v in out["workbenchBlock"]["cases"].items()},
                  "simplePressureRelaxationBound": "setSimplePressureRelaxation" in out["boundSetters"]}))
