"""Masked particle-shape probe for the installed ViennaPS 4.6.2.

One case per process, launched through ``scripts/bounded_child.py``. The probe
reproduces the official ``examples/emulation/FinFET.py`` masked pattern
verbatim so a crashing case cannot be blamed on a bespoke mask bridge.

Usage::

    python worker/probe_particle_mask.py <case.json> <output.json>

Case keys
---------
dimension    2 | 3
shape        "sphere" | "box" | "customSphere"
magnitude    signed displacement (negative etches)
halfAxes     optional 3-element list for shape=box (overrides magnitude)
radii        optional list for shape=customSphere
mask            "none" | "material" | "material-explicit" | "insertNextLevelSet"
             | "getMaterialLevelSet" | "applyToSingleMaterial" | "surface-domain"
maskMaterial name passed to addMaskMaterial (default "Mask")
maskMaterials optional list, adds one mask material per entry
registerCustomMaterial optional name; registers a custom MaterialRegistry
             material and uses it as the mask material
targetMaterial name passed to applyToSingleMaterial (default "Si")
geometry     "plane" | "trench" | "stack" | "mask-box" | "mask-empty" | "mask-full" | "finfet"
maskBox      optional [minimum, maximum] overriding the built-in mask box
insertMode   "material" (default) | "mask" (domain.insertMask)
duration     null for the two-argument Process overload, else a float
process      "two-arg" | "three-arg"
boundary     "reflective" | "periodic"
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import viennaps as ps


CUSTOM_MATERIALS: dict[str, ps.Material] = {}


def resolve_material(name: str) -> ps.Material:
    if name in CUSTOM_MATERIALS:
        return CUSTOM_MATERIALS[name]
    by_attr = getattr(ps.Material, name, None)
    by_registry = ps.MaterialRegistry.instance().findMaterial(name)
    if by_attr is None and by_registry is None:
        raise RuntimeError(f"material {name!r} is not registered")
    if by_attr is not None and by_registry is not None and by_attr != by_registry:
        raise RuntimeError(f"material handle mismatch for {name!r}: {by_attr} != {by_registry}")
    return by_attr if by_attr is not None else by_registry


def describe_material(value: ps.Material) -> dict:
    info = {"repr": repr(value), "str": str(value)}
    for attribute in ("kind", "legacyId", "isBuiltIn", "isCustom", "builtIn"):
        try:
            info[attribute] = getattr(value, attribute)()
        except Exception as error:  # pragma: no cover - diagnostic only
            info[attribute] = f"<error: {error}>"
    return info


def build_domain(case: dict):
    dimension = int(case["dimension"])
    geometry = str(case.get("geometry", "plane"))
    boundary = str(case.get("boundary", "reflective"))
    boundary_type = (ps.BoundaryType.REFLECTIVE_BOUNDARY if boundary == "reflective"
                     else ps.BoundaryType.PERIODIC_BOUNDARY)
    if dimension == 2:
        domain = ps.Domain(0.05, 2.0, boundary_type)
    else:
        domain = ps.Domain(0.05, 2.0, 2.0, boundary_type)
    ps.MakePlane(domain, height=0.0, material=resolve_material("Si"), addToExisting=False).apply()
    if geometry == "trench":
        ps.MakeTrench(domain=domain, trenchWidth=0.6, trenchDepth=0.4, trenchTaperAngle=0.0,
                      maskHeight=0.2, maskTaperAngle=0.0, halfTrench=False,
                      material=resolve_material("Si"),
                      maskMaterial=resolve_material("Mask")).apply()
    elif geometry == "stack":
        # Matches the workbench stack semantics: substrate + layer + mask layer.
        ps.MakePlane(domain, height=0.3, material=resolve_material("SiO2"),
                     addToExisting=True).apply()
        ps.MakePlane(domain, height=0.5, material=resolve_material("Si3N4"),
                     addToExisting=True).apply()
        if case.get("stackMask", True):
            minimum = [0.4, -1.0, 0.45] if dimension == 3 else [0.4, 0.45]
            maximum = [1.2, 3.0, 0.8] if dimension == 3 else [1.2, 0.8]
            mask_level_set = ps.ls.Domain(domain.getGrid())
            ps.ls.MakeGeometry(mask_level_set, ps.ls.Box(minimum, maximum)).apply()
            domain.insertNextLevelSetAsMaterial(mask_level_set, resolve_material("Mask"))
    elif geometry in {"mask-box", "finfet", "mask-empty", "mask-full"}:
        # Official 4.6.2 FinFET construction: ps.ls.Domain + ps.ls.MakeGeometry
        # + ps.ls.Box, inserted as the Mask material level set.
        if geometry == "finfet":
            ps.MakePlane(domain, height=0.4, material=resolve_material("Si"),
                         addToExisting=True).apply()
            minimum = [0.4, -1.0, 0.35] if dimension == 3 else [0.4, -1.0]
            maximum = [1.2, 3.0, 0.9] if dimension == 3 else [1.2, 3.0]
        elif geometry == "mask-empty":
            minimum = maximum = None
        elif geometry == "mask-full":
            minimum = [-2.0, -2.0, -2.0] if dimension == 3 else [-2.0, -2.0]
            maximum = [2.0, 2.0, 2.0] if dimension == 3 else [2.0, 2.0]
        else:
            minimum = [0.4, -1.0, -0.1] if dimension == 3 else [0.4, -1.0]
            maximum = [1.2, 3.0, 0.6] if dimension == 3 else [1.2, 3.0]
        if case.get("maskBox") is not None:
            minimum, maximum = case["maskBox"]
        mask_level_set = ps.ls.Domain(domain.getGrid())
        if minimum is not None:
            ps.ls.MakeGeometry(mask_level_set, ps.ls.Box(minimum, maximum)).apply()
        mask_material = resolve_material(str(case.get("maskMaterial", "Mask")))
        if case.get("insertMode") == "mask":
            domain.insertMask(mask_level_set, mask_material)
        else:
            domain.insertNextLevelSetAsMaterial(mask_level_set, mask_material)
    return domain


def mask_material_names(case: dict) -> list[str]:
    explicit = case.get("maskMaterials")
    if explicit:
        return [str(name) for name in explicit]
    return [str(case.get("maskMaterial", "Mask"))]


def raw_mask(domain, case: dict):
    """Build a raw viennals mask level set exactly as the probe asks."""
    mask_mode = str(case.get("mask", "none"))
    if mask_mode == "getMaterialLevelSet":
        return ps.ls.Domain(domain.getMaterialLevelSet(resolve_material(mask_material_names(case)[0])))
    if mask_mode == "surface-domain":
        return ps.ls.Domain(domain.getSurface())
    return None


def build_distribution(case: dict, domain):
    dimension = int(case["dimension"])
    shape = str(case.get("shape", "sphere"))
    mask_mode = str(case.get("mask", "none"))
    magnitude = float(case.get("magnitude", 0.2))
    half_axes = case.get("halfAxes")
    if half_axes is not None:
        half_axes = [float(value) for value in half_axes]
    else:
        half_axes = [magnitude] * 3

    mask = raw_mask(domain, case)
    if shape == "box":
        model = ps.BoxDistribution(half_axes, mask) if mask is not None else ps.BoxDistribution(half_axes)
    elif shape == "sphere":
        model = ps.SphereDistribution(magnitude, mask) if mask is not None else ps.SphereDistribution(magnitude)
    elif shape == "customSphere":
        radii = [float(value) for value in case["radii"]]
        model = ps.CustomSphereDistribution(radii, mask) if mask is not None else ps.CustomSphereDistribution(radii)
    else:
        raise ValueError(f"unknown shape {shape!r}")

    if mask_mode in {"material", "material-explicit", "insertNextLevelSet"}:
        for name in mask_material_names(case):
            model.addMaskMaterial(resolve_material(name))
    elif mask_mode == "applyToSingleMaterial":
        model.applyToSingleMaterial(resolve_material(str(case.get("targetMaterial", "Si"))))
    return model


def summary(domain, dimension: int) -> dict:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    points = mesh.getNodes()
    cell_data = mesh.getCellData()
    material_ids: list[int] = []
    for index in range(cell_data.getScalarDataSize()):
        if str(cell_data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = [int(value) for value in cell_data.getScalarData(index)]
    mapping = domain.getMaterialMap()
    profile = []
    if points:
        for bucket in range(10):
            lower = -1.0 + 0.2 * bucket
            upper = lower + 0.2
            subset = [point for point in points if lower <= float(point[0]) < upper]
            if subset:
                profile.append({
                    "x": round(lower, 1),
                    "points": len(subset),
                    "zMin": round(float(min(point[dimension - 1] for point in subset)), 6),
                    "zMax": round(float(max(point[dimension - 1] for point in subset)), 6),
                })
    return {
        "boundingBox": [[round(float(min(point[axis] for point in points)), 6),
                         round(float(max(point[axis] for point in points)), 6)]
                        for axis in range(dimension)] if points else None,
        "pointCount": len(points),
        "surfaceMaterials": sorted(set(material_ids)),
        "levelSetCount": int(domain.getNumberOfLevelSets()),
        "levelSetPointCounts": [int(level_set.getNumberOfPoints()) for level_set in domain.getLevelSets()],
        "materialIdsByLevelSetIndex": [int(mapping.getMaterialIdAtIdx(i)) for i in range(mapping.size())],
        "materialHandlesByLevelSetIndex": [str(mapping.getMaterialAtIdx(i)) for i in range(mapping.size())],
        "cellCount": len(mesh.getLines() if dimension == 2 else mesh.getTriangles()),
        "xProfile": profile,
    }


def main() -> None:
    case = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    target = Path(sys.argv[2])
    dimension = int(case["dimension"])
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    ps.Logger.setLogLevel(ps.LogLevel.DEBUG if case.get("debug") else ps.LogLevel.INFO)
    custom_name = case.get("registerCustomMaterial")
    if custom_name:
        handle = ps.MaterialRegistry.instance().registerMaterial(str(custom_name))
        if not handle.isCustom():
            raise RuntimeError(f"registered material {custom_name!r} is not custom")
        CUSTOM_MATERIALS[str(custom_name)] = handle
        case.setdefault("maskMaterial", str(custom_name))

    domain = build_domain(case)
    mask_mode = str(case.get("mask", "none"))
    mask_material = resolve_material(str(case.get("maskMaterial", "Mask")))
    mask_level_set = domain.getMaterialLevelSet(mask_material)
    diagnostics = {
        "requestedMaskMode": mask_mode,
        "maskMaterial": describe_material(mask_material),
        "targetMaterial": describe_material(resolve_material(str(case.get("targetMaterial", "Si")))),
        "materialsInDomain": sorted(str(value) for value in domain.getMaterialsInDomain()),
        "materialLevelSetIsNone": mask_level_set is None,
        "materialLevelSetPoints": int(mask_level_set.getNumberOfPoints())
        if mask_level_set is not None else None,
        "before": summary(domain, dimension),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "versions": {"viennaps": ps.version, "viennals": ps.ls.version},
        "case": case,
        "diagnostics": diagnostics,
    }
    # Persist the pre-run record first: a SIGSEGV then leaves a usable partial file.
    target.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    started = time.monotonic()
    model = build_distribution(case, domain)
    record["nativeClass"] = type(model).__name__
    pin_cpu = bool(case.get("pinCpu", True))
    if str(case.get("process", "three-arg")) == "two-arg":
        process = ps.Process(domain, model)
        record["processCall"] = "ps.Process(domain, model).apply()"
    else:
        duration = float(case.get("duration", 0.0))
        process = ps.Process(domain, model, duration)
        record["processCall"] = f"ps.Process(domain, model, {duration!r}).apply()"
    if pin_cpu:
        # Mirror the worker: AUTO resolves a flux engine and initialises CUDA
        # even for a geometric distribution. ``pinCpu: false`` reproduces the
        # raw default for evidence on hosts where CUDA is broken.
        process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    record["processBackend"] = "CPU_DISK" if pin_cpu else "AUTO"
    process.apply()
    elapsed = time.monotonic() - started

    record["after"] = summary(domain, dimension)
    record["elapsedSeconds"] = round(elapsed, 4)
    record["status"] = "ok"
    target.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "nativeClass": type(model).__name__, "elapsedSeconds": round(elapsed, 4)}))


if __name__ == "__main__":
    main()
