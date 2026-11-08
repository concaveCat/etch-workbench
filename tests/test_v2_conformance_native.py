"""Native conformance test for the installed isotropic plugin.

Unlike ``tests/test_v2_conformance.py`` (synthetic variants and dummy handles),
this test loads the discovered plugin registry, resolves the real
``isotropic``/``native`` variant, and runs it twice against a fresh 2D plane.
Strict determinism is checked with a geometry digest computed from the
committed native surface mesh, so two identical runs must reproduce the same
surface, not just the same metadata.

No server or worker process is involved. The conformance run rebuilds an
ephemeral lock under ``var/`` from the currently discovered manifests so it
exercises the live plugin code; the checked-in ``plugins.lock`` is verified by
the plugin-lifecycle tests.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import viennaps as ps

from process_models.plugins import default_plugin_dirs, load_registry, write_lock
from process_models.v2.conformance import check_variant
from process_models.v2.contract import ProcessContext
from process_models.v2.engine import EngineCapabilities, StaticEngine, StaticEngineProvider
from process_models.v2.state import LevelSetState, MaterialState, ProcessState
from worker.simulate import surface_state

ROOT = Path(__file__).resolve().parents[1]
PROCESS_ID = "isotropic"
VARIANT_ID = "native"
GRID_DELTA = 0.1
X_EXTENT = 1.0
ENGINE_FINGERPRINT = "test-native-conformance"
VIENNAPS_VERSION = str(ps.__version__)


def _plane_project() -> dict[str, Any]:
    """A complete schema-5 project describing the 2D plane state."""
    return {
        "schemaVersion": 5,
        "name": "native conformance 2D plane",
        "dimension": 2,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": GRID_DELTA, "xExtent": X_EXTENT, "xBoundary": "reflective"},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [],
        "solver": {"timeStepRatio": 0.25, "raysPerPoint": 32, "cpuThreads": 1},
    }


def _isotropic_step() -> dict[str, Any]:
    return {
        "id": "step_iso_etch",
        "name": "isotropic etch",
        "enabled": True,
        "operation": "etch",
        "model": "isotropic",
        "rateMagnitude": 0.3,
        "duration": 1.0,
        "rateMode": "uniform",
    }


def _plane_state() -> ProcessState:
    """Build a fresh 2D plane state with native geometry and materials."""
    ps.setDimension(2)
    domain = ps.Domain(
        gridDelta=GRID_DELTA,
        xExtent=X_EXTENT,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    mapping = domain.getMaterialMap()
    order = tuple(int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size()))
    return ProcessState(
        geometry=LevelSetState(handle=domain, dimension=2, grid_delta=GRID_DELTA),
        materials=MaterialState(order=order),
    )


def _provider() -> StaticEngineProvider:
    capabilities = EngineCapabilities(
        engine_id="viennaps",
        version=VIENNAPS_VERSION,
        kind="native",
        backends=("cpu",),
    )
    return StaticEngineProvider([StaticEngine(capabilities=capabilities, handle=ps, fingerprint=ENGINE_FINGERPRINT)])


class NativeIsotropicConformanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        var_dir = ROOT / "var"
        var_dir.mkdir(exist_ok=True)
        cls._lock_dir = tempfile.TemporaryDirectory(dir=var_dir, prefix="test-conformance-native-")
        lock_path = Path(cls._lock_dir.name) / "plugins.lock"
        plugin_dirs = default_plugin_dirs()
        write_lock(plugin_dirs, lock_path)
        cls.registry = load_registry(plugin_dirs, lock_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._lock_dir.cleanup()

    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")

    def test_native_isotropic_passes_with_matching_geometry_digest(self) -> None:
        project = _plane_project()
        step = _isotropic_step()
        resolved = self.registry.get_variant(PROCESS_ID, VARIANT_ID)
        params = resolved.process.parse_step(project, step)
        self.assertIsNotNone(params)
        self.assertIsInstance(params, resolved.process.params_model)

        context = ProcessContext(dimension=2, grid_delta=GRID_DELTA, project=project, step=step)
        digests: list[str] = []

        def digest(_result: Any, committed_state: ProcessState) -> str:
            value = surface_state(committed_state.geometry.handle, 2)["digest"]
            digests.append(value)
            return value

        initial_digest = surface_state(_plane_state().geometry.handle, 2)["digest"]

        report = check_variant(
            self.registry,
            _provider(),
            PROCESS_ID,
            VARIANT_ID,
            params,
            context,
            _plane_state,
            digest=digest,
        )

        self.assertTrue(report.passed, report.issues)
        self.assertEqual(len(digests), 2)
        self.assertEqual(digests[0], digests[1])
        self.assertNotEqual(digests[0], initial_digest)

        determinism = report.details["determinism"]
        self.assertTrue(determinism["checked"])
        self.assertEqual(determinism["compared_with"], "digest")
        self.assertEqual(determinism["differences"], [])
        self.assertEqual(report.details["plan_identity"]["engine_id"], "viennaps")
        self.assertEqual(report.details["state"]["geometry"]["dimension"], 2)


if __name__ == "__main__":
    unittest.main()
