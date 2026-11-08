"""Lifecycle tests for plugin manifests, lock verification, and the loader.

These tests are hermetic: the in-tree plugin is exercised read-only, and every
mutating case builds a temporary plugin directory. No server or data directory
is involved. The worker bridge test runs one tiny native plane step to prove
that ``run_v2_step`` resolves the isotropic process through
``process_models.plugins.load_registry`` instead of hardcoded registration.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from dataclasses import fields
from pathlib import Path
from string import Template
from types import SimpleNamespace
from unittest import mock

import viennaps as ps

from plugins.isotropic import IsotropicNativeVariant, IsotropicProcessSpec, parse_step
from process_models.plugins import (
    PLUGIN_API_VERSION,
    PluginLifecycleError,
    PluginManifest,
    default_plugin_dirs,
    discover_manifests,
    load_registry,
    read_lock,
    read_manifest,
    verify_lock,
    write_lock,
)
from process_models.v2.contract import ProcessSpec
from process_models.v2.registry import ProcessRegistry, compute_implementation_hash
from tests.test_worker_v2_isotropic import VIENNAPS_VERSION, isotropic_step, plane_domain, plane_project
from worker.process_models_v2 import run_v2_step

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIRS_ENV = "VIENNAPS_WORKBENCH_PLUGIN_DIRS"
PLUGIN_LOCK_ENV = "VIENNAPS_WORKBENCH_PLUGIN_LOCK"

PLUGIN_SOURCE = Template(
    '''"""Temporary lifecycle-test plugin."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from process_models.v2.contract import (
    EngineVariant,
    Issue,
    ProcessContext,
    ProcessSpec,
    VariantCapabilities,
    VariantRegistration,
)
from process_models.v2.engine import EngineRequirement
from process_models.v2.state import ProcessState


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thickness: float = Field(gt=0)


class NativeVariant(EngineVariant):
    variant_id = "$variant_id"
    version = "$variant_version"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry"}),
        writes=frozenset({"geometry"}),
    )
    engine = EngineRequirement(kind="native", engine_id="dummy")
    implementation_hash = None

    def validate(self, ctx: ProcessContext, params: Params) -> list[Issue]:
        return []

    def execute(self, state: ProcessState, params: Params, ctx: ProcessContext, engine):
        raise NotImplementedError("temporary test plugin")


SPEC = ProcessSpec(
    id="$plugin_id",
    version="$plugin_version",
    label="Temporary lifecycle plugin",
    params_model=Params,
    dimensions=(2, 3),
    operations=("etch",),
)


def register(registry):
    registry.register_process(SPEC)
    registry.register_variant(VariantRegistration(process_id=SPEC.id, variant=NativeVariant()))
    return SPEC
'''
)


class _TempPluginCase(unittest.TestCase):
    """Per-test temp directories, sys.path, and environment isolation."""

    def setUp(self) -> None:
        self._sys_path = list(sys.path)
        self._env = {name: os.environ.get(name) for name in (PLUGIN_DIRS_ENV, PLUGIN_LOCK_ENV)}
        self._temp_dirs: list[tempfile.TemporaryDirectory[str]] = []
        self._package_names: list[str] = []

    def tearDown(self) -> None:
        sys.path[:] = self._sys_path
        for package_name in self._package_names:
            for module_name in list(sys.modules):
                if module_name == package_name or module_name.startswith(package_name + "."):
                    sys.modules.pop(module_name, None)
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for temporary in reversed(self._temp_dirs):
            temporary.cleanup()

    def temp_dir(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self._temp_dirs.append(temporary)
        return Path(temporary.name)

    def write_manifest(
        self,
        plugin_dir: Path,
        *,
        plugin_id: str,
        version: str,
        entrypoint: str,
        api: int = 1,
        description: str | None = "Temporary lifecycle plugin",
        extra: str = "",
        raw: str | None = None,
    ) -> Path:
        plugin_dir.mkdir(parents=True, exist_ok=True)
        path = plugin_dir / "plugin.toml"
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
            return path
        lines = [
            f"api = {api}",
            f'id = "{plugin_id}"',
            f'version = "{version}"',
            f'entrypoint = "{entrypoint}"',
        ]
        if description is not None:
            lines.append(f'description = "{description}"')
        text = "\n".join(lines) + "\n" + extra
        path.write_text(text, encoding="utf-8")
        return path

    def make_plugin(
        self,
        base: Path,
        *,
        plugin_id: str = "temp_plugin",
        version: str = "0.1.0",
        variant_id: str = "native",
        variant_version: str = "1.0.0",
        package_name: str | None = None,
        manifest_kwargs: dict | None = None,
    ) -> tuple[Path, str]:
        package = package_name or f"tmp_plugin_{uuid.uuid4().hex}"
        self._package_names.append(package)
        plugin_dir = base / package
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
        (plugin_dir / "impl.py").write_text(
            PLUGIN_SOURCE.substitute(
                plugin_id=plugin_id,
                plugin_version=version,
                variant_id=variant_id,
                variant_version=variant_version,
            ),
            encoding="utf-8",
        )
        options = {
            "plugin_id": plugin_id,
            "version": version,
            "entrypoint": f"{package}.impl:register",
        }
        options.update(manifest_kwargs or {})
        self.write_manifest(plugin_dir, **options)
        return plugin_dir, package


class InTreePluginTests(unittest.TestCase):
    def test_api_version_and_manifest(self) -> None:
        self.assertEqual(PLUGIN_API_VERSION, 1)
        manifest = read_manifest(ROOT / "plugins" / "isotropic" / "plugin.toml")
        self.assertEqual(manifest.api, 1)
        self.assertEqual(manifest.id, "isotropic")
        self.assertEqual(manifest.version, "1.0.0")
        self.assertEqual(manifest.entrypoint, "plugins.isotropic:register")
        self.assertIsInstance(manifest, PluginManifest)

    def test_default_dirs_use_repo_plugins_and_honour_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PLUGIN_DIRS_ENV, None)
            self.assertEqual(default_plugin_dirs(), ((ROOT / "plugins").resolve(),))
        with mock.patch.dict(os.environ, {PLUGIN_DIRS_ENV: "plugins" + os.pathsep + str(ROOT / "plugins")}):
            self.assertEqual(
                default_plugin_dirs(),
                ((ROOT / "plugins").resolve(), (ROOT / "plugins").resolve()),
            )

    def test_discovery_finds_the_in_tree_isotropic_plugin(self) -> None:
        records = discover_manifests()
        keys = [(record.manifest.id, record.manifest.version) for record in records]
        self.assertIn(("isotropic", "1.0.0"), keys)
        isotropic = next(record for record in records if record.manifest.id == "isotropic")
        self.assertEqual(isotropic.plugin_dir, (ROOT / "plugins" / "isotropic").resolve())
        self.assertEqual(isotropic.manifest_path, isotropic.plugin_dir / "plugin.toml")

    def test_checked_in_lock_verifies_and_pins_computed_hashes(self) -> None:
        verify_lock()
        lock = read_lock()
        self.assertEqual(lock.schema_version, 1)
        self.assertEqual(lock.api_version, 1)
        entry = lock.entries()[("isotropic", "1.0.0")]
        self.assertEqual(entry.entrypoint, "plugins.isotropic:register")
        variant = entry.variants["native"]
        self.assertEqual(variant.version, "1.0.0")
        self.assertRegex(variant.implementation_hash, r"^[0-9a-f]{64}$")

        registry = load_registry()
        resolved = registry.get_variant("isotropic", "native", expected_process_version="1.0.0")
        self.assertEqual(resolved.process.id, "isotropic")
        self.assertEqual(resolved.process_version, "1.0.0")
        self.assertEqual(resolved.variant_version, "1.0.0")
        self.assertEqual(
            variant.implementation_hash,
            compute_implementation_hash(resolved.variant.variant),
        )

    def test_missing_lock_fails_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "plugins.lock"
            with self.assertRaises(PluginLifecycleError):
                load_registry(lock_path=missing)
            with self.assertRaises(PluginLifecycleError):
                verify_lock(lock_path=missing)

    def test_process_spec_declares_default_variant_and_one_parse_hook(self) -> None:
        self.assertEqual(IsotropicProcessSpec.default_variant, "native")
        self.assertIs(IsotropicProcessSpec.parse_step, parse_step)
        self.assertFalse(hasattr(IsotropicProcessSpec, "matches_step"))
        self.assertFalse(hasattr(IsotropicProcessSpec, "params_from_step"))

        registry = load_registry()
        step = isotropic_step("etch", rate=0.2, duration=0.5)
        resolved = registry.resolve_step(plane_project(2), step)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        spec, params = resolved
        self.assertIs(spec, IsotropicProcessSpec)
        self.assertEqual(params.operation, "etch")
        self.assertEqual(params.rateMagnitude, 0.2)

        material = isotropic_step("etch", rate=0.0, duration=0.5)
        material["rateMode"] = "material"
        material["materialRates"] = {"Si": 0.2}
        material_params = parse_step(plane_project(2), material)
        self.assertIsNotNone(material_params)
        assert material_params is not None
        self.assertEqual(material_params.rateMode, "material")
        self.assertEqual(material_params.rateMagnitude, 0.0)
        material_resolved = registry.resolve_step(plane_project(2), material)
        self.assertIsNotNone(material_resolved)

        unsupported = isotropic_step("etch", rate=0.2, duration=0.5)
        unsupported["isotropicMethod"] = "geometric"
        self.assertIsNone(parse_step(plane_project(2), unsupported))
        self.assertIsNone(registry.resolve_step(plane_project(2), unsupported))

    def test_project_api_has_no_module_path_or_callable_field(self) -> None:
        process_fields = {item.name for item in fields(ProcessSpec)}
        self.assertFalse({"entrypoint", "module", "callable", "path", "import"} & process_fields)
        self.assertFalse(hasattr(ProcessSpec, "entrypoint"))
        # The manifest is the only object that names an implementation module.
        manifest = read_manifest(ROOT / "plugins" / "isotropic" / "plugin.toml")
        self.assertIn(":", manifest.entrypoint)


class ManifestAndLockTamperTests(_TempPluginCase):
    def _copy_isotropic(self, base: Path) -> Path:
        plugin_dir = base / "isotropic"
        plugin_dir.mkdir()
        shutil.copy(ROOT / "plugins" / "isotropic" / "plugin.toml", plugin_dir / "plugin.toml")
        return plugin_dir

    def _lock_for(self, base: Path, lock_path: Path) -> None:
        self.assertEqual(write_lock([base], lock_path), lock_path)

    def test_strict_manifest_rejects_unknown_keys_and_bad_api(self) -> None:
        base = self.temp_dir()
        plugin_dir = base / "bad"
        self.write_manifest(
            plugin_dir,
            plugin_id="bad",
            version="0.1.0",
            entrypoint="some_module:register",
            extra="unknownField = true\n",
        )
        with self.assertRaises(PluginLifecycleError):
            read_manifest(plugin_dir / "plugin.toml")

        self.write_manifest(
            plugin_dir,
            plugin_id="bad",
            version="0.1.0",
            entrypoint="some_module:register",
            api=2,
        )
        with self.assertRaises(PluginLifecycleError):
            read_manifest(plugin_dir / "plugin.toml")

    def test_manifest_rejects_malformed_entrypoints(self) -> None:
        base = self.temp_dir()
        plugin_dir = base / "bad"
        for entrypoint in ("module", "module:", ":register", "module:factory.register", "module : register"):
            with self.subTest(entrypoint=entrypoint):
                self.write_manifest(
                    plugin_dir,
                    plugin_id="bad",
                    version="0.1.0",
                    entrypoint=entrypoint,
                )
                with self.assertRaises(PluginLifecycleError):
                    read_manifest(plugin_dir / "plugin.toml")

    def test_tampered_manifest_bytes_are_rejected(self) -> None:
        base = self.temp_dir()
        plugin_dir = self._copy_isotropic(base)
        lock_path = base / "plugins.lock"
        self._lock_for(base, lock_path)

        with (plugin_dir / "plugin.toml").open("a", encoding="utf-8") as handle:
            handle.write("\n")
        with self.assertRaises(PluginLifecycleError):
            load_registry([base], lock_path)

    def test_tampered_manifest_identity_is_rejected(self) -> None:
        base = self.temp_dir()
        plugin_dir = self._copy_isotropic(base)
        lock_path = base / "plugins.lock"
        self._lock_for(base, lock_path)

        self.write_manifest(
            plugin_dir,
            plugin_id="other-isotropic",
            version="1.0.0",
            entrypoint="plugins.isotropic:register",
        )
        with self.assertRaises(PluginLifecycleError):
            load_registry([base], lock_path)

    def test_tampered_manifest_version_and_entrypoint_are_rejected(self) -> None:
        base = self.temp_dir()
        plugin_dir = self._copy_isotropic(base)
        lock_path = base / "plugins.lock"
        self._lock_for(base, lock_path)

        self.write_manifest(
            plugin_dir,
            plugin_id="isotropic",
            version="2.0.0",
            entrypoint="plugins.isotropic:register",
        )
        with self.assertRaises(PluginLifecycleError):
            load_registry([base], lock_path)

        self.write_manifest(
            plugin_dir,
            plugin_id="isotropic",
            version="1.0.0",
            entrypoint="plugins.isotropic:not_a_registration",
        )
        with self.assertRaises(PluginLifecycleError):
            load_registry([base], lock_path)

    def test_tampered_manifest_api_is_rejected(self) -> None:
        base = self.temp_dir()
        plugin_dir = self._copy_isotropic(base)
        lock_path = base / "plugins.lock"
        self._lock_for(base, lock_path)

        self.write_manifest(
            plugin_dir,
            plugin_id="isotropic",
            version="1.0.0",
            entrypoint="plugins.isotropic:register",
            api=2,
        )
        with self.assertRaises(PluginLifecycleError):
            load_registry([base], lock_path)

    def test_missing_manifest_is_rejected(self) -> None:
        base = self.temp_dir()
        plugin_dir = self._copy_isotropic(base)
        lock_path = base / "plugins.lock"
        self._lock_for(base, lock_path)

        (plugin_dir / "plugin.toml").unlink()
        with self.assertRaises(PluginLifecycleError):
            load_registry([base], lock_path)

    def test_tampered_plugin_module_changes_the_implementation_hash(self) -> None:
        base = self.temp_dir()
        plugin_dir, _ = self.make_plugin(base)
        lock_path = base / "plugins.lock"
        self._lock_for(base, lock_path)
        load_registry([base], lock_path)  # the first import is now cached

        with (plugin_dir / "impl.py").open("a", encoding="utf-8") as handle:
            handle.write("\n# tampered implementation\n")
        with self.assertRaises(PluginLifecycleError):
            load_registry([base], lock_path)
        with self.assertRaises(PluginLifecycleError):
            verify_lock([base], lock_path)

    def test_unknown_entrypoint_module_is_rejected(self) -> None:
        base = self.temp_dir()
        plugin_dir = base / "missing"
        self.write_manifest(
            plugin_dir,
            plugin_id="missing",
            version="0.1.0",
            entrypoint="module_does_not_exist_xyz:register",
        )
        with self.assertRaises(PluginLifecycleError):
            write_lock([base], base / "plugins.lock")

    def test_duplicate_id_version_across_directories_is_rejected(self) -> None:
        first = self.temp_dir()
        second = self.temp_dir()
        self.make_plugin(first / "plugins", plugin_id="duplicate", version="1.0.0")
        self.make_plugin(second / "plugins", plugin_id="duplicate", version="1.0.0")
        with self.assertRaises(PluginLifecycleError):
            write_lock([first / "plugins", second / "plugins"], first / "plugins.lock")


class TempPluginRoundTripTests(_TempPluginCase):
    def test_implementation_files_hash_is_path_independent(self) -> None:
        first = self.temp_dir() / "first.py"
        second = self.temp_dir() / "second.py"
        payload = b"VALUE = 1\n"
        first.write_bytes(payload)
        second.write_bytes(payload)
        with mock.patch.object(
            IsotropicNativeVariant,
            "implementation_files",
            (("engine/helper.py", str(first)),),
        ):
            one = compute_implementation_hash(IsotropicNativeVariant())
        with mock.patch.object(
            IsotropicNativeVariant,
            "implementation_files",
            (("engine/helper.py", str(second)),),
        ):
            two = compute_implementation_hash(IsotropicNativeVariant())
        self.assertEqual(one, two)

    def test_missing_declared_implementation_file_fails_closed(self) -> None:
        with mock.patch.object(
            IsotropicNativeVariant,
            "implementation_files",
            (("engine/missing.py", "/definitely/not/a/file.py"),),
        ):
            with self.assertRaises(ValueError):
                compute_implementation_hash(IsotropicNativeVariant())

    def test_plugin_root_need_not_be_named_plugins(self) -> None:
        base = self.temp_dir()
        operator = base / "operator"
        operator.mkdir()
        plugin_dir, _ = self.make_plugin(operator)
        lock_path = base / "plugins.lock"
        write_lock([operator], lock_path)
        verify_lock([operator], lock_path)
        (plugin_dir / "step.py").write_text("VALUE = 1\n", encoding="utf-8")
        with self.assertRaises(PluginLifecycleError):
            verify_lock([operator], lock_path)

    def test_changing_any_plugin_package_file_invalidates_lock(self) -> None:
        base = self.temp_dir()
        plugin_root = base / "plugins"
        plugin_root.mkdir()
        plugin_dir, _ = self.make_plugin(plugin_root)
        lock_path = base / "plugins.lock"
        write_lock([plugin_root], lock_path)
        verify_lock([plugin_root], lock_path)
        (plugin_dir / "step.py").write_text("VALUE = 1\n", encoding="utf-8")
        with self.assertRaises(PluginLifecycleError):
            verify_lock([plugin_root], lock_path)

    def test_write_lock_and_load_registry_round_trip(self) -> None:
        base = self.temp_dir()
        plugin_dir, _ = self.make_plugin(base / "plugins", plugin_id="temp_plugin", version="0.1.0")
        lock_path = base / "plugins.lock"

        written = write_lock([base / "plugins"], lock_path)
        self.assertEqual(written, lock_path)
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schemaVersion"], 1)
        entry = payload["plugins"][0]
        self.assertEqual(entry["id"], "temp_plugin")
        self.assertEqual(entry["version"], "0.1.0")
        self.assertEqual(entry["manifestSha256"], hashlib.sha256((plugin_dir / "plugin.toml").read_bytes()).hexdigest())

        registry = load_registry([base / "plugins"], lock_path)
        resolved = registry.get_variant("temp_plugin", "native", expected_process_version="0.1.0")
        self.assertEqual(resolved.variant_version, "1.0.0")
        self.assertEqual(
            resolved.implementation_hash,
            entry["variants"]["native"]["implementationHash"],
        )

        verify_lock([base / "plugins"], lock_path)

    def test_env_discovery_and_lock_override(self) -> None:
        base = self.temp_dir()
        self.make_plugin(base / "plugins", plugin_id="env_plugin", version="0.1.0")
        lock_path = base / "custom.lock"
        write_lock([base / "plugins"], lock_path)

        with mock.patch.dict(
            os.environ,
            {PLUGIN_DIRS_ENV: str(base / "plugins"), PLUGIN_LOCK_ENV: str(lock_path)},
        ):
            self.assertEqual(default_plugin_dirs(), ((base / "plugins").resolve(),))
            verify_lock()
            registry = load_registry()
        self.assertIn("env_plugin", {process.id for process in registry.list_processes()})

    def test_multiple_versions_of_one_id_are_allowed(self) -> None:
        base = self.temp_dir() / "plugins"
        self.make_plugin(
            base,
            plugin_id="multi",
            version="0.1.0",
            variant_id="native_v1",
        )
        self.make_plugin(
            base,
            plugin_id="multi",
            version="0.2.0",
            variant_id="native_v2",
        )
        lock_path = base.parent / "plugins.lock"
        write_lock([base], lock_path)
        registry = load_registry([base], lock_path)
        self.assertEqual(registry.get_process("multi", "0.1.0").version, "0.1.0")
        self.assertEqual(registry.get_process("multi", "0.2.0").version, "0.2.0")
        self.assertEqual(registry.get_variant("multi", "native_v1").variant_version, "1.0.0")
        self.assertEqual(registry.get_variant("multi", "native_v2").variant_version, "1.0.0")

    def test_lock_with_extra_or_missing_plugin_entry_is_rejected(self) -> None:
        base = self.temp_dir()
        self.make_plugin(base / "plugins", plugin_id="entry_plugin", version="0.1.0")
        lock_path = base / "plugins.lock"
        write_lock([base / "plugins"], lock_path)

        original = json.loads(lock_path.read_text(encoding="utf-8"))
        extra = json.loads(json.dumps(original))
        extra["plugins"].append(dict(extra["plugins"][0], id="ghost"))
        lock_path.write_text(json.dumps(extra), encoding="utf-8")
        with self.assertRaises(PluginLifecycleError):
            load_registry([base / "plugins"], lock_path)

        missing = json.loads(json.dumps(original))
        missing["plugins"] = []
        lock_path.write_text(json.dumps(missing), encoding="utf-8")
        with self.assertRaises(PluginLifecycleError):
            load_registry([base / "plugins"], lock_path)


class WorkerBridgeLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.setDimension(2)

    def test_bridge_resolves_isotropic_from_the_discovered_registry(self) -> None:
        discovered = load_registry()
        with mock.patch("worker.process_models_v2.load_execution_registry", return_value=discovered) as loader:
            with tempfile.TemporaryDirectory() as tmp:
                ctx = SimpleNamespace(
                    project=plane_project(2),
                    ps=ps,
                    run_dir=Path(tmp),
                    ps_version=VIENNAPS_VERSION,
                    native_engine_fingerprint=None,
                )
                outcome = run_v2_step(ctx, isotropic_step("etch", rate=0.1, duration=0.5), plane_domain(2))
        loader.assert_called_once_with()
        self.assertEqual(outcome.plan.process.id, "isotropic")
        self.assertEqual(outcome.plan.variant.variant_id, "native")
        self.assertEqual(outcome.metadata["operation"], "etch")

    def test_bridge_accepts_a_caller_supplied_registry(self) -> None:
        discovered = load_registry()
        with mock.patch("worker.process_models_v2.load_execution_registry") as loader:
            with tempfile.TemporaryDirectory() as tmp:
                ctx = SimpleNamespace(
                    project=plane_project(2),
                    ps=ps,
                    run_dir=Path(tmp),
                    ps_version=VIENNAPS_VERSION,
                    native_engine_fingerprint=None,
                )
                outcome = run_v2_step(
                    ctx,
                    isotropic_step("etch", rate=0.1, duration=0.5),
                    plane_domain(2),
                    registry=discovered,
                )
        loader.assert_not_called()
        self.assertEqual(outcome.plan.process.id, "isotropic")

    def test_bridge_has_no_hardcoded_fallback_registration(self) -> None:
        empty = ProcessRegistry()
        with mock.patch("worker.process_models_v2.load_execution_registry", return_value=empty):
            with self.assertRaises(RuntimeError):
                ctx = SimpleNamespace(
                    project=plane_project(2),
                    ps=ps,
                    run_dir=Path(tempfile.mkdtemp()),
                    ps_version=VIENNAPS_VERSION,
                    native_engine_fingerprint=None,
                )
                run_v2_step(ctx, isotropic_step("etch", rate=0.1, duration=0.5), plane_domain(2))


if __name__ == "__main__":
    unittest.main()
