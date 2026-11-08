"""Dispatch matrix over the combined plugin + built-in execution registry."""

from __future__ import annotations

import json
import unittest

from pydantic import BaseModel

from process_models.execution import load_execution_registry, resolve_dispatch
from process_models.v2.catalog import execution_catalog
from process_models.v2.contract import ProcessSpec
from process_models.v2.dispatch import dispatch_step
from process_models.v2.registry import ProcessRegistry

PROJECT = {
    "schemaVersion": 5,
    "dimension": 2,
    "domain": {"gridDelta": 0.02, "xExtent": 1.0},
    "solver": {"timeStepRatio": 0.25, "raysPerPoint": 8, "cpuThreads": 1},
}


def _isotropic() -> dict:
    return {
        "id": "isotropic_step_01",
        "name": "etch",
        "enabled": True,
        "operation": "etch",
        "model": "isotropic",
        "rateMagnitude": 0.1,
        "duration": 0.1,
        "rateMode": "uniform",
    }


def _implant() -> dict:
    return {
        "id": "implant_step_01",
        "name": "implant",
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
        "cellSet": {"height": 0.2, "substrateDepth": 0.1},
    }


def _planarize() -> dict:
    return {
        "id": "planarize_step_01",
        "name": "cmp",
        "enabled": True,
        "model": "planarize",
        "cutoffHeight": 0.1,
    }


class _DummyParams(BaseModel):
    value: int = 1


class DispatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_execution_registry()

    def test_combined_registry_contains_plugins_and_geometry(self) -> None:
        ids = {descriptor.id for descriptor in self.registry.list_processes()}
        self.assertIn("isotropic", ids)
        self.assertIn("implant", ids)
        self.assertIn("planarize", ids)
        self.assertIn("gdsBoolean", ids)
        self.assertGreaterEqual(len(ids), 28)

    def test_execution_catalog_is_json_safe_and_kind_aware(self) -> None:
        catalog = execution_catalog(self.registry)
        payload = json.loads(json.dumps(catalog, allow_nan=False))
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertGreaterEqual(len(payload["steps"]), 28)
        by_id = {step["id"]: step for step in payload["steps"]}
        self.assertEqual(by_id["planarize"]["executionKind"], "geometry")
        self.assertEqual(by_id["implant"]["executionKind"], "field")
        self.assertEqual(by_id["isotropic"]["executionKind"], "process")
        self.assertNotIn("parse_step", json.dumps(by_id))

    def test_descriptor_exposes_execution_kind(self) -> None:
        by_id = {descriptor.id: descriptor for descriptor in self.registry.list_processes()}
        self.assertEqual(by_id["isotropic"].execution_kind, "process")
        self.assertEqual(by_id["implant"].execution_kind, "field")
        self.assertEqual(by_id["planarize"].execution_kind, "geometry")

    def test_dispatch_kind_matrix(self) -> None:
        cases = (
            (_isotropic(), "process", "isotropic"),
            (_implant(), "field", "implant"),
            (_planarize(), "geometry", "planarize"),
        )
        for step, kind, process_id in cases:
            with self.subTest(model=step["model"]):
                decision = dispatch_step(self.registry, PROJECT, step)
                self.assertEqual(decision.kind, kind)
                self.assertTrue(decision.claimed)
                assert decision.spec is not None
                self.assertEqual(decision.spec.id, process_id)
                self.assertIsNotNone(decision.params)

    def test_unknown_model_is_an_explicit_legacy_decision(self) -> None:
        decision = dispatch_step(self.registry, PROJECT, {"model": "notAProcess"})
        self.assertEqual(decision.kind, "legacy")
        self.assertFalse(decision.claimed)
        self.assertIn("notAProcess", decision.reason)

    def test_malformed_claimed_step_is_invalid_not_legacy(self) -> None:
        decision = dispatch_step(self.registry, PROJECT, {"model": "implant"})
        self.assertEqual(decision.kind, "invalid")
        self.assertFalse(decision.claimed)
        self.assertTrue(decision.error)

    def test_ambiguous_match_is_invalid(self) -> None:
        registry = ProcessRegistry()
        for process_id in ("dummy.a", "dummy.b"):
            registry.register_process(
                ProcessSpec(
                    id=process_id,
                    version="1.0.0",
                    label=process_id,
                    params_model=_DummyParams,
                    dimensions=(2,),
                    operations=("test",),
                    parse_step=lambda project, step: _DummyParams(),
                )
            )
        decision = dispatch_step(registry, {}, {"model": "dummy"})
        self.assertEqual(decision.kind, "invalid")
        self.assertIn("more than one", decision.error or "")

    def test_resolve_dispatch_can_use_a_caller_supplied_registry(self) -> None:
        decision = resolve_dispatch(PROJECT, _planarize(), registry=self.registry)
        self.assertEqual(decision.kind, "geometry")
        assert decision.spec is not None
        self.assertEqual(decision.spec.id, "planarize")


if __name__ == "__main__":
    unittest.main()
