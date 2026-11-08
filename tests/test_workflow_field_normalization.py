"""Route-compile normalization for contiguous field sequences."""

from __future__ import annotations

import unittest

from backend.models import (
    AdvectionSettingsV7,
    Domain,
    FixedRandom,
    RayTracingSettingsV7,
    SolverV7,
    Units,
)
from backend.workflow import FlowRevisionV1, GeometryBindingV1, compile_flow
from backend.workflow.compiler import _normalize_field_sequence

UNITS = Units(length="um", time="s", rate="um/s", angle="deg")


def _solver() -> SolverV7:
    return SolverV7(
        timeStepRatio=0.25,
        raysPerPoint=32,
        cpuThreads=1,
        random=FixedRandom(mode="fixed", seed=314159),
        enginePreference="cpu",
        timeoutSeconds=60,
        advection=AdvectionSettingsV7(),
        rayTracing=RayTracingSettingsV7(),
    )


def _binding() -> GeometryBindingV1:
    return GeometryBindingV1(
        schemaVersion=1,
        bindingId="bind_plane_01",
        name="2D plane binding",
        dimension=2,
        units=UNITS,
        domain=Domain(gridDelta=0.02, xExtent=1.0, xBoundary="reflective"),
        geometry={"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
    )


def _cell_set(*, height: float, depth: float, periodic: bool = False) -> dict:
    return {
        "height": height,
        "substrateDepth": depth,
        "coverMaterial": "GAS",
        "periodic": periodic,
        "format": "json",
    }


def _implant(*, height: float, depth: float, periodic: bool = False, step_id: str = "implant_step_01") -> dict:
    return {
        "id": step_id,
        "name": "P implant",
        "enabled": True,
        "model": "implant",
        "species": "P",
        "material": "Si",
        "energyKeV": 10.0,
        "doseCm2": 1.0e15,
        "tiltDeg": 7.0,
        "doseControl": "WaferDose",
        "profile": {
            "source": "manual",
            "kind": "pearsonIV",
            "head": {"mu": 0.03, "sigma": 0.012, "beta": 1.15, "gamma": 6.2},
            "lateralMu": 0.0,
            "lateralSigma": 0.005,
        },
        "cellSet": _cell_set(height=height, depth=depth, periodic=periodic),
    }


def _anneal(*, height: float, depth: float, periodic: bool = False, step_id: str = "anneal_step_01") -> dict:
    return {
        "id": step_id,
        "name": "anneal",
        "enabled": True,
        "model": "anneal",
        "mode": "GaussSeidel",
        "schedule": {"kind": "isothermal", "temperatureK": 1273.15, "durationSeconds": 1.0},
        "diffusivity": {"kind": "arrhenius", "D0": 1.23e13, "Ea_eV": 3.566},
        "diffusionMaterials": ["Si"],
        "blockingMaterials": [],
        "speciesLabel": "P_total",
        "activeLabel": "P_active",
        "solidActivation": {"solubilityC0": 1.521e2, "solubilityEa_eV": 0.7102},
        "cellSet": _cell_set(height=height, depth=depth, periodic=periodic),
    }


def _analysis(*, height: float, depth: float, periodic: bool = False, step_id: str = "analysis_step_01") -> dict:
    return {
        "id": step_id,
        "name": "analysis",
        "enabled": True,
        "model": "analysis",
        "netDoping": {
            "donorLabels": ["P_active"],
            "acceptorLabels": [],
            "outputLabel": "net_doping",
        },
        "sheetResistance": [{"concentrationLabel": "P_active", "carrier": "electron"}],
        "cellSet": _cell_set(height=height, depth=depth, periodic=periodic),
    }


def _flow(*steps: dict) -> FlowRevisionV1:
    return FlowRevisionV1(
        schemaVersion=1,
        flowId="flow_field_01",
        revision=1,
        name="field flow",
        steps=list(steps),
        solver=_solver(),
    )


class FieldNormalizationTest(unittest.TestCase):
    def test_height_and_depth_take_the_sequence_maximum(self) -> None:
        compiled = compile_flow(
            _flow(
                _implant(height=0.2, depth=0.1),
                _anneal(height=0.3, depth=0.2),
                _analysis(height=0.25, depth=0.15),
            ),
            _binding(),
        )
        for step in compiled.project.steps:
            self.assertEqual(step.cellSet.height, 0.3)
            self.assertEqual(step.cellSet.substrateDepth, 0.2)
            self.assertEqual(step.cellSet.coverMaterial, "GAS")
            self.assertFalse(step.cellSet.periodic)

    def test_equivalent_sequences_share_project_and_compile_identity(self) -> None:
        first = compile_flow(
            _flow(
                _implant(height=0.2, depth=0.1),
                _anneal(height=0.3, depth=0.2),
                _analysis(height=0.25, depth=0.15),
            ),
            _binding(),
        )
        second = compile_flow(
            _flow(
                _implant(height=0.3, depth=0.2),
                _anneal(height=0.2, depth=0.1),
                _analysis(height=0.25, depth=0.15),
            ),
            _binding(),
        )
        self.assertEqual(first.compileRecord.fullProjectHash, second.compileRecord.fullProjectHash)
        self.assertEqual(first.compileRecord.compileId, second.compileRecord.compileId)

    def test_periodic_conflict_fails_compilation(self) -> None:
        with self.assertRaises(ValueError) as caught:
            compile_flow(
                _flow(
                    _implant(height=0.2, depth=0.1, periodic=False),
                    _anneal(height=0.2, depth=0.1, periodic=True),
                ),
                _binding(),
            )
        self.assertIn("periodic", str(caught.exception))

    def test_cover_conflict_fails_normalization(self) -> None:
        payloads = [
            {"id": "implant_step_01", "cellSet": _cell_set(height=0.2, depth=0.1)},
            {"id": "anneal_step_01", "cellSet": _cell_set(height=0.2, depth=0.1)},
        ]
        payloads[1]["cellSet"]["coverMaterial"] = "SiO2"
        with self.assertRaises(ValueError) as caught:
            _normalize_field_sequence(payloads, start=0)
        self.assertIn("coverMaterial", str(caught.exception))

    def test_a_non_field_step_splits_the_sequence(self) -> None:
        etch = {
            "id": "etch_step_0001",
            "name": "etch",
            "enabled": True,
            "operation": "etch",
            "model": "isotropic",
            "rateMagnitude": 0.1,
            "duration": 0.1,
            "rateMode": "uniform",
        }
        compiled = compile_flow(
            _flow(
                _implant(height=0.2, depth=0.1),
                etch,
                _anneal(height=0.3, depth=0.2),
            ),
            _binding(),
        )
        self.assertEqual(compiled.project.steps[0].cellSet.height, 0.2)
        self.assertEqual(compiled.project.steps[0].cellSet.substrateDepth, 0.1)
        self.assertEqual(compiled.project.steps[2].cellSet.height, 0.3)
        self.assertEqual(compiled.project.steps[2].cellSet.substrateDepth, 0.2)

    def test_disabled_field_step_does_not_break_the_sequence(self) -> None:
        disabled = _anneal(height=0.9, depth=0.9, step_id="anneal_step_disabled")
        disabled["enabled"] = False
        compiled = compile_flow(
            _flow(
                _implant(height=0.2, depth=0.1),
                disabled,
                _analysis(height=0.3, depth=0.2),
            ),
            _binding(),
        )
        self.assertEqual(compiled.project.steps[0].cellSet.height, 0.3)
        self.assertEqual(compiled.project.steps[2].cellSet.height, 0.3)
        self.assertEqual(compiled.project.steps[1].cellSet.height, 0.9)


if __name__ == "__main__":
    unittest.main()
