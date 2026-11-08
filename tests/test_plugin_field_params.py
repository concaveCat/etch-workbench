"""Parameter and dispatch tests for the implant/anneal/analysis plugins."""

from __future__ import annotations

import unittest

from plugins.analysis import parse_step as parse_analysis
from plugins.analysis import register as register_analysis
from plugins.anneal import parse_step as parse_anneal
from plugins.anneal import register as register_anneal
from plugins.implant import parse_step as parse_implant
from plugins.implant import register as register_implant
from process_models.v2.contract import ProcessContext
from process_models.v2.registry import ProcessRegistry

PROJECT = {
    "dimension": 2,
    "domain": {"gridDelta": 0.02, "xExtent": 1.0, "xBoundary": "reflective"},
}


def _cell_set(**overrides):
    value = {
        "height": 0.2,
        "substrateDepth": 0.1,
        "coverMaterial": "GAS",
        "periodic": False,
        "format": "json",
    }
    value.update(overrides)
    return value


def _implant(**overrides):
    value = {
        "id": "implant_step_01",
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
        "cellSet": _cell_set(),
    }
    value.update(overrides)
    return value


def _anneal(**overrides):
    value = {
        "id": "anneal_step_01",
        "model": "anneal",
        "mode": "GaussSeidel",
        "schedule": {"kind": "isothermal", "temperatureK": 1273.15, "durationSeconds": 1.0},
        "diffusivity": {"kind": "arrhenius", "D0": 1.23e13, "Ea_eV": 3.566},
        "diffusionMaterials": ["Si"],
        "blockingMaterials": [],
        "speciesLabel": "P_total",
        "activeLabel": "P_active",
        "solidActivation": {"solubilityC0": 1.521e2, "solubilityEa_eV": 0.7102},
        "cellSet": _cell_set(),
    }
    value.update(overrides)
    return value


def _analysis(**overrides):
    value = {
        "id": "analysis_step_01",
        "model": "analysis",
        "netDoping": {
            "donorLabels": ["P_active"],
            "acceptorLabels": [],
            "outputLabel": "net_doping",
        },
        "sheetResistance": [{"concentrationLabel": "P_active", "carrier": "electron"}],
        "cellSet": _cell_set(),
    }
    value.update(overrides)
    return value


class FieldParamTest(unittest.TestCase):
    def test_each_parser_claims_only_its_own_model(self) -> None:
        self.assertIsNotNone(parse_implant(PROJECT, _implant()))
        self.assertIsNone(parse_implant(PROJECT, _anneal()))
        self.assertIsNone(parse_analysis(PROJECT, _implant()))
        self.assertIsNotNone(parse_analysis(PROJECT, _analysis()))

    def test_table_profile_is_rejected(self) -> None:
        step = _implant(profile={"source": "table", "assetId": "0" * 64})
        with self.assertRaises(ValueError):
            parse_implant(PROJECT, step)

    def test_rotation_and_reserved_labels_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_implant(PROJECT, _implant(rotationDeg=7.0))
        with self.assertRaises(ValueError):
            parse_implant(PROJECT, _implant(labels={"concentration": "Material"}))
        with self.assertRaises(ValueError):
            parse_anneal(PROJECT, _anneal(activeLabel="FillingFraction"))

    def test_non_gas_cover_and_explicit_implicit_solver_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_implant(PROJECT, _implant(cellSet=_cell_set(coverMaterial="SiO2")))
        with self.assertRaises(ValueError):
            parse_anneal(PROJECT, _anneal(mode="Explicit", implicitSolver={"maxIterations": 10}))

    def test_variant_does_not_reject_missing_depth_before_state_is_known(self) -> None:
        registry = ProcessRegistry()
        register_implant(registry)
        register_anneal(registry)
        register_analysis(registry)
        raw = _implant(cellSet=_cell_set(substrateDepth=None))
        spec, params = registry.resolve_step(PROJECT, raw)
        context = ProcessContext(
            dimension=2,
            grid_delta=0.02,
            project=PROJECT,
            step=raw,
        )
        issues = registry.get_variant(spec.id, "native").variant.variant.validate(context, params)
        self.assertFalse(any(issue.path == "cellSet.substrateDepth" for issue in issues))

    def test_periodic_cell_set_requires_a_periodic_domain(self) -> None:
        registry = ProcessRegistry()
        register_implant(registry)
        raw = _implant(cellSet=_cell_set(periodic=True))
        spec, params = registry.resolve_step(PROJECT, raw)
        context = ProcessContext(
            dimension=2,
            grid_delta=0.02,
            project=PROJECT,
            step=raw,
        )
        issues = registry.get_variant(spec.id, "native").variant.variant.validate(context, params)
        self.assertTrue(any(issue.path == "cellSet.periodic" for issue in issues))

    def test_implant_default_labels_match_the_worker_convention(self) -> None:
        params = parse_implant(PROJECT, _implant())
        assert params.labels is not None
        self.assertEqual(params.labels.concentration, "P_total")
        self.assertIsNone(params.labels.damage)
        with_damage = parse_implant(
            PROJECT,
            _implant(
                damage={
                    "kind": "hobler",
                    "projectedRange": 0.027,
                    "verticalSigma": 0.018,
                    "lambdaValue": 0.024,
                    "defectsPerIon": 142.9,
                    "lateralSigma": 0.013,
                    "lateralDeltaSigma": -0.02,
                }
            ),
        )
        assert with_damage.labels is not None
        self.assertEqual(with_damage.labels.damage, "P_damage")
        self.assertEqual(with_damage.labels.lastDamage, "P_damage_last")


if __name__ == "__main__":
    unittest.main()
