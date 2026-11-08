"""Probe the installed ViennaPS 4.6.2 oxidation surface for B5.

The probe answers three questions without touching the running Workbench:

1. which candidate names are actually bound by the installed Python package;
2. whether the four accepted ``advancedOxidation`` setters change a real
   native surface hash at a schema-valid 2D configuration;
3. whether Si100/Si110/Si111 produce distinct native surfaces in 2D and 3D.

Every run goes through the same validation and ``build_specialized_model``
path as the worker, in a fresh process per case (ViennaPS 4.6.2 keeps
dimension state process-wide). Run with::

    .venv/bin/python worker/probe_oxidation_advanced_native.py

The JSON printed on stdout is the evidence quoted in
docs/oxidation-advanced-contract.md.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import viennaps as ps  # noqa: E402


CANDIDATE_NAMES = (
    "OxidationParameters", "psOxidation", "psProcessContext",
    "oxidantFlux", "reactionRate", "stabilityFactor", "volumeExpansion",
    "redoStabilityCheck", "calculateError", "maxCellTravelDistance",
    "useFluxSolver", "fluxSolver", "fluxMultiplier", "solverStabilityFactor",
    "trapLayer", "diffusionCellSet", "maxGridPoints", "timeStepRatio",
    "finiteVolume", "fluxAtBoundary",
)


def api_report() -> dict:
    oxidation_setters = sorted(
        name for name in dir(ps.Oxidation)
        if name.startswith("set") and not name.startswith("_"))
    bound_setters = {
        name: (name in oxidation_setters)
        for name in (
            "setCFLFactor", "setMechanicsTolerance", "setCouplingTolerance",
            "setSimplePressureRelaxation", "setMaxGridPoints", "setSolveBounds",
            "setCouplingIterations", "setMechanicsIterations",
            "setPressureIterations", "setStokesIterations",
            "setPressureTolerance", "setStokesTolerance",
            "setSimpleVelocityRelaxation", "setGpuMode",
            "setGpuPreconditioner", "setMaskParameters",
            "setMaskTractionRelaxation", "setMaskUnilateralContact",
            "setOxidationParameters",
        )
    }
    return {
        "viennapsVersion": ps.version,
        "siliconOrientationValues": {
            name: int(getattr(ps.SiliconOrientation, name))
            for name in ("Si100", "Si110", "Si111", "PolySi")
        },
        "moduleAttributePresence": {
            name: hasattr(ps, name) for name in CANDIDATE_NAMES
        },
        "oxidationSetterPresence": bound_setters,
        "oxidationSetterCount": len(oxidation_setters),
        "hasNativeGetter": {
            name: hasattr(ps.Oxidation, name)
            for name in ("getCFLFactor", "getMechanicsTolerance",
                         "getCouplingTolerance", "getSimplePressureRelaxation",
                         "getMaxGridPoints", "getSolveBounds")
        },
        "readbackAvailable": hasattr(ps.Oxidation, "estimatePlanarOxideThickness"),
    }


def planar_estimates() -> dict:
    model = ps.Oxidation()
    oxidant = getattr(ps.OxidantType, "Dry", None)
    if oxidant is None:
        oxidant = getattr(ps.OxidantType, "DRY")
    model.setOxidant(oxidant)
    model.setTemperature(1000.0)
    model.setPressure(1.0)
    model.setTime(0.5)
    values = {}
    for name in ("Si100", "Si110", "Si111", "PolySi"):
        model.setOrientation(getattr(ps.SiliconOrientation, name))
        values[name] = float(model.estimatePlanarOxideThickness(0.0))
    return values


def run_case(dimension: int, *, delta: float, oxide: float,
             time_hours: float, time_step_hours: float,
             orientation: str = "Si100",
             advanced: dict | None = None) -> dict:
    command = [
        sys.executable, str(ROOT / "tests" / "fixtures" / "oxidation_surface_hash.py"),
        "--dimension", str(dimension), "--delta", str(delta), "--extent", "1.0",
        "--oxide", str(oxide), "--orientation", orientation,
        "--time-hours", str(time_hours),
        "--time-step-hours", str(time_step_hours),
    ]
    if advanced is not None:
        command.extend(["--advanced", json.dumps(advanced)])
    completed = subprocess.run(command, cwd=ROOT, capture_output=True,
                               text=True, timeout=300, check=False)
    if completed.returncode != 0:
        return {"error": completed.stderr[-2000:]}
    return json.loads(completed.stdout.strip().splitlines()[-1])


def main() -> int:
    cases = {
        "2d_defaults": dict(dimension=2, delta=0.01, oxide=0.05,
                            time_hours=0.166, time_step_hours=0.03),
        "2d_cflFactor_0.05": dict(dimension=2, delta=0.01, oxide=0.05,
                                  time_hours=0.166, time_step_hours=0.03,
                                  advanced={"cflFactor": 0.05}),
        "2d_mechanicsTolerance_0.05": dict(
            dimension=2, delta=0.01, oxide=0.05,
            time_hours=0.166, time_step_hours=0.03,
            advanced={"mechanicsTolerance": 0.05}),
        "2d_couplingTolerance_0.2": dict(
            dimension=2, delta=0.02, oxide=0.06,
            time_hours=0.166, time_step_hours=0.03,
            advanced={"couplingTolerance": 0.2}),
        "2d_simplePressureRelaxation_0.1": dict(
            dimension=2, delta=0.01, oxide=0.05,
            time_hours=0.166, time_step_hours=0.03,
            advanced={"simplePressureRelaxation": 0.1}),
        "2d_Si110": dict(dimension=2, delta=0.01, oxide=0.05,
                         time_hours=0.166, time_step_hours=0.03,
                         orientation="Si110"),
        "2d_Si111": dict(dimension=2, delta=0.01, oxide=0.05,
                         time_hours=0.166, time_step_hours=0.03,
                         orientation="Si111"),
        "3d_Si100": dict(dimension=3, delta=0.05, oxide=0.15,
                         time_hours=0.1, time_step_hours=0.03),
        "3d_Si110": dict(dimension=3, delta=0.05, oxide=0.15,
                         time_hours=0.1, time_step_hours=0.03,
                         orientation="Si110"),
        "3d_Si111": dict(dimension=3, delta=0.05, oxide=0.15,
                         time_hours=0.1, time_step_hours=0.03,
                         orientation="Si111"),
    }
    results = {}
    for name, kwargs in cases.items():
        results[name] = run_case(**kwargs)
    output = {
        "schemaVersion": 1,
        "engine": "viennaps 4.6.2 + viennals 5.8.5",
        "api": api_report(),
        "planarOxideEstimatesUmAt1000CDry0.5hr": planar_estimates(),
        "cases": results,
    }
    print(json.dumps(output, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
