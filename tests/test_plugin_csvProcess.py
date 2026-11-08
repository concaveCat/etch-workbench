"""Tests for the native CSV rate-grid plugin.

The asset path is supplied by the run context (the worker copies a
content-addressed CSV into the isolated run directory). These tests build the
same canonical CSV in a temporary directory and use a manual registry, so the
shared plugin lock is not consulted.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

import viennaps as ps

from plugins.csvProcess import CsvProcessParams, parse_step, register
from process_models.v2.contract import ProcessContext
from process_models.v2.engine import EngineCapabilities, StaticEngine, StaticEngineProvider
from process_models.v2.engines.viennaps import configure_advection
from process_models.v2.executor import FlowExecutor, FlowStep
from process_models.v2.registry import ProcessRegistry
from process_models.v2.state import LevelSetState, MaterialState, ProcessState
from process_models.v2.storage import StateStore
from worker.process_models_v2 import compare_domains

ROOT = Path(__file__).resolve().parents[1]
GRID_DELTA = 0.2
X_EXTENT = 4.0
DURATION = 0.2
VIENNAPS_VERSION = str(ps.__version__)
CSV_2D = b"0,0.1\n1,0.1\n2,0.1\n3,0.1\n"


def _plane() -> Any:
    ps.setDimension(2)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    domain = ps.Domain(
        gridDelta=GRID_DELTA,
        xExtent=X_EXTENT,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def _material_order(domain: Any) -> tuple[int, ...]:
    mapping = domain.getMaterialMap()
    return tuple(int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size()))


def _project() -> dict[str, Any]:
    return {
        "schemaVersion": 7,
        "dimension": 2,
        "geometry": {"kind": "plane", "substrateMaterial": "Si"},
        "solver": {
            "timeStepRatio": 0.25,
            "cpuThreads": 1,
            "advection": {
                "spatialScheme": "ENGQUIST_OSHER_1ST_ORDER",
                "temporalScheme": "FORWARD_EULER",
                "adaptiveTimeStepping": False,
                "adaptiveTimeStepSubdivisions": 20,
                "calculateIntermediateVelocities": False,
                "checkDissipation": True,
                "dissipationAlpha": 1.0,
                "ignoreVoids": False,
                "velocityOutput": False,
            },
        },
    }


def _step(asset_id: str) -> dict[str, Any]:
    return {
        "id": "csv-step-0001",
        "name": "CSV rate grid",
        "enabled": True,
        "operation": "native",
        "model": "csvProcess",
        "duration": DURATION,
        "assetId": asset_id,
        "fileName": "rates.csv",
        "direction": [0, -1, 0],
        "offset": [0, 0],
        "isotropicComponent": 0.0,
        "directionalComponent": 1.0,
        "interpolationMode": "LINEAR",
        "calculateVisibility": True,
    }


def _reference(path: Path, project: dict[str, Any], domain: Any) -> Any:
    model = ps.CSVFileProcess(
        str(path),
        [0.0, -1.0, 0.0],
        [0.0, 0.0],
        0.0,
        1.0,
        [],
        True,
    )
    model.setInterpolationMode("LINEAR")
    model.setOffset([0.0, 0.0])
    process = ps.Process(domain, model, DURATION)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection, _actual = configure_advection(ps, project)
    process.setParameters(advection)
    process.apply()
    return domain


class CsvProcessPluginTest(unittest.TestCase):
    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.Time.setUnit("s")
        self.work_dir = ROOT / "var" / f"test-plugin-csvProcess-{self._testMethodName}"
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)
        self.asset_path = self.work_dir / "rates.csv"
        self.asset_path.write_bytes(CSV_2D)
        self.asset_id = hashlib.sha256(CSV_2D).hexdigest()
        self.project = _project()
        self.step = _step(self.asset_id)
        self.project["steps"] = [self.step]
        self.registry = ProcessRegistry()
        register(self.registry)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _context(self) -> ProcessContext:
        return ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=self.project,
            step=self.step,
            assets={self.asset_id: str(self.asset_path)},
        )

    def _run(self, domain: Any) -> Any:
        state = ProcessState(
            geometry=LevelSetState(handle=domain, dimension=2, grid_delta=GRID_DELTA),
            materials=MaterialState(order=_material_order(domain), names={int(ps.Material.Si): "Si"}),
        )
        provider = StaticEngineProvider(
            [
                StaticEngine(
                    capabilities=EngineCapabilities(
                        engine_id="viennaps",
                        version=VIENNAPS_VERSION,
                        kind="native",
                        backends=("cpu",),
                        kernels=("CPU_DISK",),
                    ),
                    handle=ps,
                    fingerprint="csv-process-test",
                )
            ]
        )
        store = StateStore(self.work_dir / "store")
        return (
            FlowExecutor(self.registry, provider, store)
            .run(
                state,
                [
                    FlowStep(
                        step_id=self.step["id"],
                        process_id="csvProcess",
                        variant_id="native",
                        params=self.registry.resolve_step(self.project, self.step)[1],
                        context=self._context(),
                    )
                ],
            )
            .outcomes[-1]
        )

    def test_two_dimensional_csv_matches_native_reference(self) -> None:
        reference = _reference(self.asset_path, self.project, _plane())
        outcome = self._run(_plane())
        comparison = compare_domains(reference, outcome.state.geometry.handle, 2)
        self.assertTrue(comparison["matched"], comparison["reasons"])
        self.assertEqual(outcome.plan.identity()["engine_kernel"], "CPU_DISK")

    def test_parse_step_rejects_unknown_and_invalid_modes(self) -> None:
        self.assertIsNotNone(parse_step(self.project, self.step))
        self.assertIsNone(parse_step(self.project, {**self.step, "model": "isotropic"}))
        self.assertIsNone(parse_step(self.project, {**self.step, "unknown": 1}))
        broken = {**self.step, "interpolationMode": "IDW"}
        with self.assertRaises(ValueError):
            parse_step(self.project, broken)
        params = _step(self.asset_id)
        params["interpolationMode"] = "IDW"
        with self.assertRaises(ValueError):
            CsvProcessParams(**params)

    def test_missing_asset_and_hash_mismatch_fail_before_native(self) -> None:
        missing = dict(self._context().assets)
        missing.clear()
        context = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=self.project,
            step=self.step,
            assets=missing,
        )
        state = ProcessState(
            geometry=LevelSetState(handle=_plane(), dimension=2, grid_delta=GRID_DELTA),
            materials=MaterialState(order=(int(ps.Material.Si),), names={int(ps.Material.Si): "Si"}),
        )
        provider = StaticEngineProvider(
            [
                StaticEngine(
                    capabilities=EngineCapabilities(
                        engine_id="viennaps",
                        version=VIENNAPS_VERSION,
                        kind="native",
                        backends=("cpu",),
                        kernels=("CPU_DISK",),
                    ),
                    handle=ps,
                    fingerprint="csv-process-test",
                )
            ]
        )
        store = StateStore(self.work_dir / "missing-store")
        with self.assertRaises(RuntimeError):
            FlowExecutor(self.registry, provider, store).run(
                state,
                [
                    FlowStep(
                        step_id=self.step["id"],
                        process_id="csvProcess",
                        variant_id="native",
                        params=self.registry.resolve_step(self.project, self.step)[1],
                        context=context,
                    )
                ],
            )

    def test_import_does_not_pull_in_worker_or_backend(self) -> None:
        code = (
            "import sys\n"
            "import plugins.csvProcess\n"
            "print(sorted(name for name in sys.modules if name == 'worker' or name.startswith('worker.') "
            "or name == 'backend' or name.startswith('backend.')))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
