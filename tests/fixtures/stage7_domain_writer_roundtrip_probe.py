"""Bounded diagnostic for surface-writer and native Domain-state side effects."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sys
from collections import Counter
from pathlib import Path

import viennaps as ps

from stage7_multiparticle_reference import make_domain, register_custom_materials, snapshot


def signature(domain, dimension):
    mesh = snapshot(domain, dimension)
    payload = json.dumps({"points": mesh["points"], "cells": mesh["cells"],
                          "materialIds": mesh["materialIds"]}, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    return {"pointCount": len(mesh["points"]), "cellCount": len(mesh["cells"]),
            "materialCounts": dict(Counter(mesh["materialIds"])),
            "materialIdsByLevelSetIndex": mesh["materialIdsByLevelSetIndex"],
            "levelSetCount": mesh["levelSetCount"], "surfaceSha256": hashlib.sha256(payload).hexdigest()}


def empty_domain(project):
    settings = project["domain"]
    boundary_name = settings.get("xBoundary", settings.get("boundaryXY"))
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if boundary_name == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
    return ps.Domain(float(settings["gridDelta"]), float(settings["xExtent"]),
                     float(settings["yExtent"]), boundary)


project_path, output_path, _engine = sys.argv[1:]
project = json.loads(Path(project_path).read_text(encoding="utf-8"))
ps.Length.setUnit("um"); ps.Time.setUnit("s")
ps.setDimension(int(project["dimension"]))
ps.setNumThreads(int(project["solver"]["cpuThreads"]))
register_custom_materials(project.get("materialDefinitions", []))
domain = make_domain(project)
states = {"afterGeometry": signature(domain, project["dimension"])}
surface_path = Path(output_path).with_suffix(".surface.vtk").resolve()
domain.saveSurfaceMesh(str(surface_path), addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=.01)
states["afterSaveSurfaceMesh"] = signature(domain, project["dimension"])
native_path = Path(output_path).with_suffix(".vpsd").resolve()
ps.Writer(domain, str(native_path)).apply()
states["afterWriter"] = signature(domain, project["dimension"])
reloaded = empty_domain(project)
ps.Reader(reloaded, str(native_path)).apply()
states["afterReader"] = signature(reloaded, project["dimension"])
result = {"viennapsModulePath": ps.__file__,
          "versions": {"viennaps": importlib.metadata.version("viennaps"),
                       "viennals": importlib.metadata.version("viennals")},
          "dimension": project["dimension"], "diagnostic": states,
          "surfacePath": str(surface_path), "statePath": str(native_path), "steps": {}}
Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
print(json.dumps({"versions": result["versions"], "dimension": result["dimension"],
                  "states": states}), flush=True)
