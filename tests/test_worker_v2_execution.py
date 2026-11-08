"""Opt-in worker execution path: v2 state bridge and claimed-step cutover."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import viennals
import viennaps as ps

from tests.test_worker_v2_isotropic import TEST_ROOT, plane_domain, trench_domain
from worker.steps.cellset import run_cell_set_step
from worker.steps.context import RunContext
from worker.steps.geometry import run_geometry_step
from worker.steps.noop import run_no_op_step
from worker.steps.process import run_process_step
from worker.v2_state import WorkerV2State


def _project(geometry: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schemaVersion": 5,
        "name": "v2 execution",
        "dimension": 2,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.2, "xExtent": 4.0, "xBoundary": "reflective"},
        "geometry": geometry or {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [],
        "solver": {
            "timeStepRatio": 0.25,
            "raysPerPoint": 32,
            "cpuThreads": 1,
            "random": {"mode": "fixed", "seed": 314159},
            "enginePreference": "cpu",
            "timeoutSeconds": 30,
        },
        "view": {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True},
    }


def _isotropic() -> dict[str, Any]:
    return {
        "id": "isotropic_step_01",
        "name": "etch",
        "enabled": True,
        "operation": "etch",
        "model": "isotropic",
        "rateMagnitude": 0.3,
        "duration": 1.0,
        "rateMode": "uniform",
    }


def _planarize() -> dict[str, Any]:
    return {
        "id": "planarize_step_01",
        "name": "cmp",
        "enabled": True,
        "model": "planarize",
        "cutoffHeight": 0.1,
    }


def _events(run_dir: Path, event_type: str) -> list[dict[str, Any]]:
    path = run_dir / "events.ndjson"
    if not path.is_file():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [record for record in records if record.get("type") == event_type]


@contextlib.contextmanager
def _execution_flag():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VIENNAPS_WORKBENCH_V2_PLUGIN_SHADOW", None)
        os.environ.pop("VIENNAPS_WORKBENCH_V2_PLUGIN_APPLY", None)
        os.environ["VIENNAPS_WORKBENCH_V2_EXECUTION"] = "1"
        yield


class WorkerV2ExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.setDimension(2)
        self.work_dir = TEST_ROOT / f"execution-{self._testMethodName}"
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _context(self, name: str, project: dict[str, Any], step: dict[str, Any], domain: Any) -> RunContext:
        run_dir = self.work_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        return RunContext(
            project=project,
            ps=ps,
            domain=domain,
            run_dir=run_dir,
            run_id=f"run-{name}",
            config={},
            actual_steps=[step],
            step_engines={},
            engine_preference="cpu",
            run_seed=None,
            selected_gpu_device=None,
            cuda_readback=None,
            gpu_sample=None,
            native_engine_fingerprint=None,
            ps_version=str(ps.__version__),
            ls_version=str(viennals.__version__),
            snapshots=[{"stepId": "initial", "materialTops": None}],
            field_state={},
            actual_execution=[],
            intermediate_outputs=[],
        )

    def test_process_step_runs_through_v2_and_commits_state(self) -> None:
        step = _isotropic()
        ctx = self._context("process", _project(), step, plane_domain(2))
        with _execution_flag():
            result = run_process_step(ctx, 1, step)
        self.assertIsNotNone(ctx.v2_state)
        assert ctx.v2_state is not None
        self.assertIs(ctx.v2_state.geometry.handle, ctx.domain)
        self.assertEqual(ctx.v2_state.materials.order, (int(ps.Material.Si),))
        self.assertIn("v2Plan", result.metadata)
        configured = _events(ctx.run_dir, "step-configured")
        self.assertEqual(len(configured), 1, configured)
        self.assertIn("v2Plan", configured[0])

    def test_field_sequence_uses_v2_cell_set_and_persists_at_its_end(self) -> None:
        cell = {
            "height": 0.5,
            "substrateDepth": 0.4,
            "coverMaterial": "GAS",
            "periodic": False,
            "format": "json",
        }
        implant = {
            "id": "implant_step_01",
            "name": "implant",
            "enabled": True,
            "model": "implant",
            "species": "P",
            "material": "Si",
            "energyKeV": 10.0,
            "doseCm2": 1.0e15,
            "tiltDeg": 7.0,
            "rotationDeg": 0.0,
            "doseControl": "WaferDose",
            "profile": {
                "source": "manual",
                "kind": "pearsonIV",
                "head": {"mu": 0.03, "sigma": 0.012, "beta": 1.15, "gamma": 6.2},
                "lateralMu": 0.0,
                "lateralSigma": 0.005,
            },
            "cellSet": cell,
        }
        anneal = {
            "id": "anneal_step_01",
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
            # A later field step may omit substrateDepth and inherit the
            # canonical lattice from the existing v2 state.
            "cellSet": {key: value for key, value in cell.items() if key != "substrateDepth"},
        }
        project = _project()
        ctx = self._context("field", project, implant, plane_domain(2))
        ctx.actual_steps = [implant, anneal]
        with _execution_flag():
            run_cell_set_step(ctx, 1, implant)
            self.assertIsNotNone(ctx.v2_state)
            assert ctx.v2_state is not None
            self.assertIsNotNone(ctx.v2_state.fields)
            first_store = ctx.run_dir / "v2" / implant["id"] / "steps" / implant["id"]
            self.assertFalse((first_store / "fields.npz").exists())
            run_cell_set_step(ctx, 2, anneal)
        self.assertIsNotNone(ctx.v2_state)
        assert ctx.v2_state is not None
        final_fields = ctx.v2_state.fields
        self.assertIsNotNone(final_fields)
        second_store = ctx.run_dir / "v2" / anneal["id"] / "steps" / anneal["id"]
        self.assertTrue((second_store / "fields.npz").exists())
        self.assertIn("P_active", final_fields.fields)

    def test_legacy_step_invalidates_field_history(self) -> None:
        cell = {
            "height": 0.5,
            "substrateDepth": 0.4,
            "coverMaterial": "GAS",
            "periodic": False,
            "format": "json",
        }
        implant = {
            "id": "implant_step_01",
            "name": "implant",
            "enabled": True,
            "model": "implant",
            "species": "P",
            "material": "Si",
            "energyKeV": 10.0,
            "doseCm2": 1.0e15,
            "tiltDeg": 7.0,
            "rotationDeg": 0.0,
            "doseControl": "WaferDose",
            "profile": {
                "source": "manual",
                "kind": "pearsonIV",
                "head": {"mu": 0.03, "sigma": 0.012, "beta": 1.15, "gamma": 6.2},
                "lateralMu": 0.0,
                "lateralSigma": 0.005,
            },
            "cellSet": cell,
        }
        anneal = {
            "id": "anneal_step_01",
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
            "cellSet": cell,
        }
        ctx = self._context("invalidate", _project(), implant, plane_domain(2))
        ctx.actual_steps = [implant, anneal]
        with _execution_flag():
            run_cell_set_step(ctx, 1, implant)
            WorkerV2State.invalidate(ctx)
            self.assertTrue(ctx.field_state.get("v2FieldInvalidated"))
            with self.assertRaises(RuntimeError) as caught:
                run_cell_set_step(ctx, 2, anneal)
        self.assertIn("cannot reuse the v2 cell set", str(caught.exception))

    def test_process_after_field_does_not_rewrite_stale_dense_arrays(self) -> None:
        cell = {
            "height": 0.5,
            "substrateDepth": 0.4,
            "coverMaterial": "GAS",
            "periodic": False,
            "format": "json",
        }
        implant = {
            "id": "implant_step_01",
            "name": "implant",
            "enabled": True,
            "model": "implant",
            "species": "P",
            "material": "Si",
            "energyKeV": 10.0,
            "doseCm2": 1.0e15,
            "tiltDeg": 7.0,
            "rotationDeg": 0.0,
            "doseControl": "WaferDose",
            "profile": {
                "source": "manual",
                "kind": "pearsonIV",
                "head": {"mu": 0.03, "sigma": 0.012, "beta": 1.15, "gamma": 6.2},
                "lateralMu": 0.0,
                "lateralSigma": 0.005,
            },
            "cellSet": cell,
        }
        anneal = {
            "id": "anneal_step_01",
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
            "cellSet": cell,
        }
        isotropic = {
            "id": "isotropic_step_01",
            "name": "etch",
            "enabled": True,
            "operation": "etch",
            "model": "isotropic",
            "rateMagnitude": 0.3,
            "duration": 0.3,
            "rateMode": "uniform",
        }
        analysis = {
            "id": "analysis_step_01",
            "name": "analysis",
            "enabled": True,
            "model": "analysis",
            "netDoping": {"donorLabels": ["P_active"], "acceptorLabels": [], "outputLabel": "net_doping"},
            "sheetResistance": [{"concentrationLabel": "P_active", "carrier": "electron"}],
            "cellSet": cell,
        }
        ctx = self._context("process-after-field", _project(), implant, plane_domain(2))
        ctx.actual_steps = [implant, anneal, isotropic, analysis]
        with _execution_flag():
            run_cell_set_step(ctx, 1, implant)
            run_cell_set_step(ctx, 2, anneal)
            run_process_step(ctx, 3, isotropic)
            process_store = ctx.run_dir / "v2" / isotropic["id"] / "steps" / isotropic["id"]
            self.assertFalse((process_store / "fields.npz").exists())
            self.assertTrue(json.loads((process_store / "state.json").read_text())["fieldsOmitted"])
            with self.assertRaises(RuntimeError) as caught:
                run_cell_set_step(ctx, 4, analysis)
        self.assertIn("different geometry", str(caught.exception))

    def test_noop_does_not_inherit_v2_execution_provenance(self) -> None:
        step = _isotropic()
        ctx = self._context("noop", _project(), step, plane_domain(2))
        with _execution_flag():
            result = run_process_step(ctx, 1, step)
            ctx.snapshots.append(result.metadata)
            zero = {**step, "id": "isotropic_step_zero", "rateMagnitude": 0.0}
            metadata = run_no_op_step(ctx, 2, zero)
        copied = json.loads((ctx.run_dir / f"{zero['id']}.json").read_text(encoding="utf-8"))
        self.assertNotIn("v2Plan", copied)
        self.assertNotIn("v2Metadata", copied)
        self.assertNotIn("v2Outputs", copied)
        self.assertNotIn("v2Plan", metadata)
        events = _events(ctx.run_dir, "v2-execution-noop")
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0]["stepId"], zero["id"])
        self.assertIn("dispatch", events[0])

    def test_geometry_step_runs_through_v2_and_preserves_state(self) -> None:
        project = _project(
            {
                "kind": "trench",
                "width": 1.6,
                "depth": 1.2,
                "taperAngle": 0.0,
                "maskHeight": 0.6,
                "maskTaperAngle": 0.0,
                "substrateMaterial": "Si",
                "maskMaterial": "Mask",
            }
        )
        step = _planarize()
        ctx = self._context("geometry", project, step, trench_domain())
        with _execution_flag():
            result = run_geometry_step(ctx, 1, step)
        self.assertEqual(result["v2Metadata"]["geometryOperation"], "planarize")
        self.assertIsNotNone(ctx.v2_state)
        assert ctx.v2_state is not None
        self.assertIs(ctx.v2_state.geometry.handle, ctx.domain)

    def test_plugin_step_envelope_runs_through_v2(self) -> None:
        step = {
            "id": "plugin_step_01",
            "name": "plugin step",
            "enabled": True,
            "model": "plugin",
            "pluginId": "isotropic",
            "params": {
                "operation": "etch",
                "rateMagnitude": 0.1,
                "duration": 1.0,
                "rateMode": "uniform",
            },
        }
        ctx = self._context("plugin-envelope", _project(), step, plane_domain(2))
        # Plugin-owned steps run through v2 even when native steps are still
        # on the opt-in legacy default.
        with mock.patch.dict(os.environ, {"VIENNAPS_WORKBENCH_V2_EXECUTION": "0"}):
            result = run_process_step(ctx, 1, step)
        self.assertIn("v2Plan", result.metadata)
        self.assertIsNotNone(ctx.v2_state)

    def test_claimed_steps_reuse_one_run_session_and_keep_per_step_replay(self) -> None:
        project = _project()
        first = _isotropic()
        second = {
            **first,
            "id": "isotropic_step_02",
            "operation": "deposit",
            "depositMaterial": "SiO2",
        }
        ctx = self._context("session", project, first, plane_domain(2))
        with _execution_flag():
            run_process_step(ctx, 1, first)
            session = ctx.v2_session
            self.assertIsNotNone(session)
            first_store = ctx.run_dir / "v2" / first["id"] / "steps" / first["id"]
            self.assertTrue((first_store / "state.json").is_file())
            self.assertTrue((first_store / "plan.json").is_file())
            self.assertTrue((first_store / "geometry.vpsd").is_file())

            run_process_step(ctx, 2, second)

        self.assertIs(ctx.v2_session, session)
        assert session is not None
        self.assertIs(session.registry, ctx.v2_session.registry)
        second_store = ctx.run_dir / "v2" / second["id"] / "steps" / second["id"]
        self.assertTrue((second_store / "state.json").is_file())
        self.assertTrue((second_store / "plan.json").is_file())
        self.assertTrue((second_store / "geometry.vpsd").is_file())
        self.assertTrue((ctx.run_dir / "v2" / second["id"] / "flow.json").is_file())

    def test_two_claimed_steps_share_one_state(self) -> None:
        project = _project()
        first = _isotropic()
        second = {
            **first,
            "id": "isotropic_step_02",
            "operation": "deposit",
            "depositMaterial": "SiO2",
        }
        ctx = self._context("two", project, first, plane_domain(2))
        with _execution_flag():
            run_process_step(ctx, 1, first)
            first_handle = ctx.domain
            run_process_step(ctx, 2, second)
        self.assertIsNot(ctx.domain, first_handle)
        self.assertIsNotNone(ctx.v2_state)
        assert ctx.v2_state is not None
        self.assertIs(ctx.v2_state.geometry.handle, ctx.domain)


if __name__ == "__main__":
    unittest.main()
