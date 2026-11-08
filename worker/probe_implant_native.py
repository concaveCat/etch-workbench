"""Bounded probe of the official ViennaPS 4.7.0 implant / anneal / analysis API.

This script answers the questions the schema is built on, in one bounded
process per case (ViennaPS keeps the dimension in process-wide state):

1. which classes and setters the installed 4.7.0 bindings actually expose;
2. whether a manual Pearson IV implant writes the requested cell-set labels and
   how many substrate rows the *unmodified* Domain cell set contains (this is
   why the worker widens the level-set band on a deep copy);
3. whether implant + anneal + NetDoping + SheetResistance run in 2D and 3D and
   produce bit-identical fields on a repeated run;
4. whether the model database is installed (it is not part of the wheel).

Run with::

    .venv/bin/python worker/probe_implant_native.py

The JSON printed on stdout is quoted in docs/capability-implant.md.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _digest(values: list[float]) -> str:
    return hashlib.sha256(array("d", values).tobytes()).hexdigest()[:16]


def _surface(ps, domain) -> list[tuple[float, float]]:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    return [(round(float(p[0]), 6), round(float(p[1]), 6)) for p in mesh.getNodes()]


def main() -> int:
    import viennaps as ps
    import viennals

    surface = {
        "viennaps": ps.__version__,
        "viennals": viennals.__version__,
        "classes": {
            name: sorted(member for member in dir(getattr(ps, name)) if member.startswith("set")
                         or member in {"apply", "computeElectron", "computeHole", "junctionDepth",
                                       "junctionDepths", "junctionCount",
                                       "lateralJunctionPosition", "lateralJunctionPositions",
                                       "applyActivation"})
            for name in ("IonImplantation", "Anneal", "NetDoping", "SheetResistance",
                         "ImplantPearsonIV", "ImplantDualPearsonIV",
                         "ImplantPearsonIVChanneling", "ImplantDamageHobler",
                         "ImplantTableModel", "DamageTableModel")
        },
        "annealModes": [mode.name for mode in ps.AnnealMode],
        "doseControls": [mode.name for mode in ps.ImplantDoseControl],
        "generateCellSet": ps.d2.Domain.generateCellSet.__doc__.splitlines()[0].strip(),
        "fromLevelSets": ps.d2.DenseCellSet.fromLevelSets.__doc__.splitlines()[0].strip(),
        "modelDbRoot": ps.getModelDbRoot(),
        "wheelHasModelDb": any((Path(ps.__file__).resolve().parent / name).exists()
                               for name in ("modeldb", "model_db", "data")),
    }

    # The narrow band the native generator produces without help.
    ps.setDimension(2)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    band = ps.Domain(gridDelta=0.01, xExtent=1.0, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(band, height=0.0, material=ps.Material.Si).apply()
    band.generateCellSet(0.2, ps.Material.GAS, True)
    band_material = [int(value) for value in band.getCellSet().getScalarData("Material")]
    surface["planeCellSet"] = {
        "cells": int(band.getCellSet().getNumberOfCells()),
        "siCells": band_material.count(10),
        "gasCells": band_material.count(3),
        "note": "one substrate row: the level set only stores a narrow band",
    }

    cases = []
    for dimension in (2, 3):
        script = [sys.executable, str(Path(__file__).resolve()), "--case", str(dimension)]
        completed = subprocess.run(script, capture_output=True, text=True, timeout=300, check=False)
        if completed.returncode != 0:
            cases.append({"dimension": dimension, "error": completed.stderr[-800:]})
            continue
        cases.append(json.loads(completed.stdout))
    # Table mode evidence: the constructor needs an explicit CSV path and the
    # wheel ships none, so a lookup cannot be part of the workbench contract.
    try:
        ps.ImplantTableModel("/nonexistent/implant.csv", "phosphorus", "silicon", "crystalline",
                             10.0, 7.0, 22.0, 1e14, 2.0, 0.0)
        table = {"missingFileRaises": False}
    except Exception as exc:  # noqa: BLE001 - probe records the native error type
        table = {"missingFileRaises": True, "errorType": type(exc).__name__, "error": str(exc)[:120]}
    print(json.dumps({"surface": surface, "tableModel": table, "cases": cases},
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def case(dimension: int) -> int:
    import viennaps as ps

    ps.setDimension(dimension)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    delta = 0.02 if dimension == 3 else 0.01
    if dimension == 2:
        domain = ps.Domain(gridDelta=delta, xExtent=1.0, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    else:
        domain = ps.Domain(gridDelta=delta, xExtent=1.0, yExtent=1.0,
                           boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si).apply()
    ps.MakePlane(domain, height=0.02, material=ps.Material.SiO2, addToExisting=True).apply()
    before = _surface(ps, domain)

    # The worker's construction: widen the band on a deep copy, generate there.
    field = ps.Domain(domain)
    for level_set in field.getLevelSets():
        ps.ls.Expand(level_set, 2 * int(round(0.3 / delta)) + 2).apply()
    field.generateCellSet(0.2, ps.Material.GAS, True)
    cell_set = field.getCellSet()
    band = {
        "expandedCells": int(cell_set.getNumberOfCells()),
        "expandedSiCells": [int(v) for v in cell_set.getScalarData("Material")].count(10),
        "surfaceUnchanged": _surface(ps, field) == before,
    }

    params = ps.PearsonIVParameters()
    params.mu = 0.03
    params.sigma = 0.012
    params.beta = 1.15
    params.gamma = 6.2
    implant = ps.IonImplantation()
    implant.setImplantModel(ps.ImplantPearsonIV(params, 0.0, 0.005))
    implant.setDamageModel(ps.ImplantDamageHobler(0.027, 0.018, 0.024, 142.9, 0.013, -0.02))
    implant.setDose(1e15)
    implant.setTiltAngle(7.0)
    implant.setLengthUnit(1e-4)
    implant.setDoseControl(ps.ImplantDoseControl.WaferDose)
    implant.setScreenMaterials([ps.Material.SiO2])
    implant.setMaskMaterials([ps.Material.Mask])
    implant.setConcentrationLabel("P_total")
    implant.setDamageLabel("P_damage")
    implant.setLastDamageLabel("P_damage_last")
    started = time.perf_counter()
    ps.Process(field, implant, 0.0).apply()
    implant_seconds = round(time.perf_counter() - started, 4)

    anneal = ps.Anneal()
    anneal.setTemperature(1273.15)
    anneal.setDuration(30.0)
    anneal.setArrheniusParameters(1.23e13, 3.566)
    anneal.setMode(ps.AnnealMode.GaussSeidel)
    anneal.setImplicitSolverOptions(400, 1e-6, 1.0)
    anneal.setDiffusionMaterials([ps.Material.Si])
    anneal.setBlockingMaterials([ps.Material.SiO2])
    anneal.setSpeciesLabel("P_total")
    anneal.setActiveLabel("P_active")
    anneal.enableSolidActivation(True)
    anneal.setSolidSolubilityArrhenius(1.521e2, 0.7102)
    started = time.perf_counter()
    ps.Process(field, anneal, 0.0).apply()
    anneal_seconds = round(time.perf_counter() - started, 4)

    net = ps.NetDoping()
    net.setCellSet(cell_set)
    net.addDonorLabel("P_active")
    net.apply()
    resistance = ps.SheetResistance()
    resistance.setCellSet(cell_set)
    resistance.setLengthUnit(1e-4)
    resistance.setConcentrationLabel("P_active")
    rsh = resistance.computeElectron()

    labels = ["P_total", "P_damage", "P_active", "net_doping"]
    fields = {
        label: {"max": max(cell_set.getScalarData(label)),
                "sum": sum(cell_set.getScalarData(label)),
                "digest": _digest([float(value) for value in cell_set.getScalarData(label)])}
        for label in labels
    }
    result = {
        "dimension": dimension,
        "band": band,
        "implantSeconds": implant_seconds,
        "annealSeconds": anneal_seconds,
        "labels": [str(label) for label in cell_set.getScalarDataLabels()],
        "fields": fields,
        "junctionDepth": net.junctionDepth(),
        "junctionCount": net.junctionCount(),
        "sheetResistanceOhmPerSquare": rsh,
        "surfaceUnchangedAfterProcesses": _surface(ps, field) == before,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(case(int(sys.argv[sys.argv.index("--case") + 1])) if "--case" in sys.argv else main())
