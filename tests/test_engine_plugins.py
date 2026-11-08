"""Engine plugin lifecycle: both ViennaPS and a custom engine are equal."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from process_models.engine_plugins import (
    EngineContext,
    EngineLock,
    EngineLockEntry,
    compute_engine_hash,
    load_engine_registry,
    read_engine_manifest,
    write_engine_lock,
)
from process_models.v2.engine import EngineRequirement, ExecutionProfile

FAKE_PLUGIN = """
from process_models.v2.engine import EngineCapabilities, StaticEngine, StaticEngineProvider


class FakePlugin:
    engine_id = "fake"
    version = "1.0.0"

    def create_provider(self, context):
        return StaticEngineProvider([
            StaticEngine(
                capabilities=EngineCapabilities(
                    engine_id="fake",
                    version="1.0.0",
                    kind="external",
                    backends=("cpu",),
                    capabilities=("geometry.levelset",),
                    kernels=("FAKE",),
                ),
                handle=object(),
                fingerprint=context.fingerprint or "fake-plugin",
            )
        ])


PLUGIN = FakePlugin()
"""


def _write_fake_engine(root: Path) -> Path:
    package = root / "fake_engine"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "plugin.py").write_text(FAKE_PLUGIN, encoding="utf-8")
    manifest_path = package / "engine.toml"
    manifest_path.write_text(
        "\n".join(
            [
                "api = 1",
                'engine_id = "fake"',
                'version = "1.0.0"',
                'entrypoint = "fake_engine.plugin:PLUGIN"',
                'description = "test engine"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    manifest = read_engine_manifest(manifest_path)
    manifest_hash, implementation_hash = compute_engine_hash(manifest_path)
    lock = EngineLock(
        (
            EngineLockEntry(
                engine_id=manifest.engine_id,
                version=manifest.version,
                entrypoint=manifest.entrypoint,
                manifest_sha256=manifest_hash,
                implementation_hash=implementation_hash,
            ),
        )
    )
    write_engine_lock(lock, root / "engines.lock")
    return package


class PluginStepEnvelopeTest(unittest.TestCase):
    def test_plugin_step_envelope_is_flattened_for_parse_step(self) -> None:
        from process_models.execution import load_execution_registry
        from process_models.v2.dispatch import dispatch_step, normalize_plugin_step

        raw = {
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
        normalized = normalize_plugin_step(raw)
        self.assertEqual(normalized["model"], "isotropic")
        decision = dispatch_step(
            load_execution_registry(),
            {"schemaVersion": 5, "dimension": 2},
            raw,
        )
        self.assertEqual(decision.kind, "process")
        self.assertEqual(decision.spec.id, "isotropic")


class InstalledEnginePluginTest(unittest.TestCase):
    def test_viennaps_loads_through_the_same_registry(self) -> None:
        registry = load_engine_registry()
        with tempfile.TemporaryDirectory() as temporary:
            provider = registry.provider(
                EngineContext(project={}, run_dir=Path(temporary), fingerprint="test-fingerprint")
            )
        capabilities = provider.engines()
        self.assertEqual(len(capabilities), 1)
        viennaps = capabilities[0]
        self.assertEqual(viennaps.engine_id, "viennaps")
        self.assertIn("CPU_DISK", viennaps.kernels)
        self.assertIn("geometry.levelset", viennaps.capabilities)


class CustomEnginePluginTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        _write_fake_engine(self.root)
        self.registry = load_engine_registry(
            engine_dirs=self.root,
            lock_path=self.root / "engines.lock",
        )

    def test_custom_engine_is_acquired_by_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as run_root:
            provider = self.registry.provider(
                EngineContext(project={}, run_dir=Path(run_root), fingerprint="fake-fingerprint")
            )
            lease = provider.acquire(
                EngineRequirement(
                    kind="external",
                    engine_id="fake",
                    min_version="1.0.0",
                    profiles=(ExecutionProfile(backend="cpu", kernel="FAKE"),),
                )
            )
            try:
                self.assertEqual(lease.engine_id, "fake")
                self.assertEqual(lease.profile.kernel, "FAKE")
                self.assertEqual(lease.fingerprint, "fake-fingerprint")
            finally:
                lease.release()

    def test_solver_can_select_a_custom_engine_and_kernel(self) -> None:
        from backend.models import ProjectV7
        from worker.process_models_v2 import compute_policy_for_step

        project = ProjectV7.model_validate(
            {
                "schemaVersion": 7,
                "name": "custom engine",
                "dimension": 2,
                "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
                "domain": {"gridDelta": 0.2, "xExtent": 4.0, "xBoundary": "reflective"},
                "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
                "steps": [],
                "solver": {
                    "timeStepRatio": 0.25,
                    "raysPerPoint": 32,
                    "cpuThreads": 1,
                    "random": {"mode": "fixed", "seed": 1},
                    "enginePreference": "cpu",
                    "engineId": "fake",
                    "engineKernel": "FAKE",
                },
                "view": {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True},
            }
        )
        ctx = SimpleNamespace(
            project=project.model_dump(mode="json", exclude_none=True),
            step_engines={},
            selected_gpu_device=None,
        )
        policy = compute_policy_for_step(ctx, {"id": "any"})
        self.assertEqual(policy.backend, "cpu")
        self.assertEqual(policy.kernel, "FAKE")

    def test_project_validation_rejects_an_uninstalled_engine_id(self) -> None:
        from backend.api_errors import project_or_422

        project = {
            "schemaVersion": 7,
            "name": "engine validation",
            "dimension": 2,
            "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
            "domain": {"gridDelta": 0.2, "xExtent": 4.0, "xBoundary": "reflective"},
            "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
            "steps": [],
            "solver": {
                "timeStepRatio": 0.25,
                "raysPerPoint": 32,
                "cpuThreads": 1,
                "random": {"mode": "fixed", "seed": 1},
                "enginePreference": "cpu",
                "engineId": "missing-engine",
                "engineKernel": "FAKE",
            },
            "view": {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True},
        }
        with self.assertRaises(Exception) as raised:
            project_or_422(project)
        self.assertIn("not installed", str(getattr(raised.exception, "detail", raised.exception)))

    def test_custom_kernel_policy_is_not_rejected_by_a_native_whitelist(self) -> None:
        from worker.process_models_v2 import compute_policy_for_step

        ctx = SimpleNamespace(step_engines={"step": "FAKE"}, selected_gpu_device=None)
        policy = compute_policy_for_step(ctx, {"id": "step"})
        self.assertEqual(policy.backend, "cpu")
        self.assertEqual(policy.kernel, "FAKE")

        auto_ctx = SimpleNamespace(step_engines={}, selected_gpu_device=None)
        auto = compute_policy_for_step(auto_ctx, {"id": "step"})
        self.assertEqual(auto.backend, "auto")
        self.assertIsNone(auto.kernel)

    def test_unknown_engine_fails_closed_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as run_root:
            provider = self.registry.provider(
                EngineContext(project={}, run_dir=Path(run_root), fingerprint="fake-fingerprint")
            )
            with self.assertRaises(Exception) as raised:
                provider.acquire(
                    EngineRequirement(
                        kind="external",
                        engine_id="missing",
                        min_version="1.0.0",
                        profiles=(ExecutionProfile(backend="cpu", kernel="FAKE"),),
                    )
                )
            self.assertIn("no engine satisfies", str(raised.exception))

    def test_lock_drift_is_rejected(self) -> None:
        plugin = self.root / "fake_engine" / "plugin.py"
        plugin.write_text(plugin.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
        with self.assertRaises(ValueError) as raised:
            load_engine_registry(engine_dirs=self.root, lock_path=self.root / "engines.lock")
        self.assertIn("implementation changed", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
