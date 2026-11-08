"""Contract tests for the phase-1 process-model plugin boundary.

These tests use a dummy in-memory variant only. They do not import ViennaPS,
start a server, or touch the data directory.
"""

from __future__ import annotations

import hashlib
import inspect
import unittest

from pydantic import BaseModel, Field, ValidationError

from process_models import (
    EngineVariant,
    Issue,
    ModelDescriptor,
    ProcessContext,
    ProcessModelRegistry,
    ProcessPlugin,
    ProcessResult,
    ProcessState,
    VariantCapabilities,
)


class DummyParams(BaseModel):
    thickness: float = Field(gt=0)
    material: str = "Si"


class NativeDummyVariant(EngineVariant):
    variant_id = "native"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=[2, 3],
        engines=["native"],
        supports_fields=False,
        supports_masks=True,
    )

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        if params.thickness > ctx.grid_delta * 100:
            return [
                Issue(
                    path="thickness",
                    message="thickness exceeds 100x the grid spacing",
                    severity="warning",
                )
            ]
        return []

    def execute(self, state: ProcessState, params: BaseModel, ctx: ProcessContext) -> ProcessResult:
        return ProcessResult(
            domain=state.domain,
            metadata={"thickness": params.thickness, "variant": self.variant_id},
            outputs=["dummy"],
        )


class NativeV2Variant(NativeDummyVariant):
    version = "2.0.0"


class SurrogateDummyVariant(EngineVariant):
    variant_id = "surrogate"
    version = "2.0.0"
    capabilities = VariantCapabilities(dimensions=[2], engines=["surrogate"], experimental=True)

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        return []

    def execute(self, state: ProcessState, params: BaseModel, ctx: ProcessContext) -> ProcessResult:
        return ProcessResult(domain=state.domain, metadata={"variant": self.variant_id})


class ExplicitHashVariant(NativeDummyVariant):
    variant_id = "explicit"
    version = "1.0.0"
    implementation_hash = "sha256:explicit"


class DummyPlugin(ProcessPlugin):
    def __init__(
        self,
        version: str = "1.0.0",
        variants: list[EngineVariant] | None = None,
        plugin_id: str = "dummy.deposition",
        label: str = "Dummy deposition",
        descriptor_id: str | None = None,
    ) -> None:
        self.id = plugin_id
        self.version = version
        self.params_model = DummyParams
        self.variants = variants if variants is not None else [NativeDummyVariant()]
        self._label = label
        self._descriptor_id = descriptor_id or plugin_id

    def describe(self) -> ModelDescriptor:
        return ModelDescriptor(
            id=self._descriptor_id,
            label=self._label,
            version=self.version,
            kind="deposition",
            operation="deposit",
            dimensions=[2, 3],
            fields=[],
        )


def _context(grid_delta: float = 0.5) -> ProcessContext:
    return ProcessContext(
        dimension=2,
        grid_delta=grid_delta,
        project={"name": "dummy"},
        step={"type": "deposit"},
        materials={"Si": 0},
    )


class ProcessModelContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ProcessModelRegistry()

    def test_register_and_get_returns_plugin(self) -> None:
        plugin = DummyPlugin()
        self.registry.register(plugin)
        self.assertIs(self.registry.get("dummy.deposition"), plugin)

    def test_multiple_versions_of_same_plugin_coexist(self) -> None:
        self.registry.register(DummyPlugin("1.0.0"))
        self.registry.register(DummyPlugin("2.0.0", variants=[NativeV2Variant()]))
        self.assertEqual(self.registry.get("dummy.deposition").version, "2.0.0")
        resolved = self.registry.resolve("dummy.deposition", "native")
        self.assertEqual(resolved.plugin_version, "2.0.0")

    def test_get_uses_highest_semver_version(self) -> None:
        for version in ("1.9.0", "1.10.0", "1.2.0"):
            self.registry.register(DummyPlugin(version))
        self.assertEqual(self.registry.get("dummy.deposition").version, "1.10.0")

    def test_get_falls_back_to_registration_order_for_non_semver_versions(self) -> None:
        for version in ("rev-a", "rev-b", "rev-c"):
            self.registry.register(DummyPlugin(version))
        self.assertEqual(self.registry.get("dummy.deposition").version, "rev-c")

    def test_resolve_returns_newest_plugin_version_with_variant(self) -> None:
        self.registry.register(DummyPlugin("1.0.0"))
        self.registry.register(DummyPlugin("2.0.0", variants=[NativeV2Variant()]))
        resolved = self.registry.resolve("dummy.deposition", "native")
        self.assertEqual(resolved.plugin_id, "dummy.deposition")
        self.assertEqual(resolved.plugin_version, "2.0.0")
        self.assertEqual(resolved.variant_id, "native")
        self.assertEqual(resolved.variant_version, "2.0.0")
        self.assertIsInstance(resolved.variant, NativeV2Variant)

    def test_resolve_honours_version_pins(self) -> None:
        self.registry.register(DummyPlugin("1.0.0"))
        self.registry.register(DummyPlugin("2.0.0", variants=[NativeV2Variant()]))

        pinned_plugin = self.registry.resolve("dummy.deposition", "native", expected_plugin_version="1.0.0")
        self.assertEqual(pinned_plugin.plugin_version, "1.0.0")
        self.assertEqual(pinned_plugin.variant_version, "1.0.0")

        pinned_both = self.registry.resolve(
            "dummy.deposition",
            "native",
            expected_plugin_version="2.0.0",
            expected_variant_version="2.0.0",
        )
        self.assertEqual(pinned_both.plugin_version, "2.0.0")
        self.assertEqual(pinned_both.variant_version, "2.0.0")

        pinned_variant = self.registry.resolve("dummy.deposition", "native", expected_variant_version="1.0.0")
        self.assertEqual(pinned_variant.plugin_version, "1.0.0")

    def test_resolve_unknown_plugin_variant_or_version_raises_key_error(self) -> None:
        self.registry.register(DummyPlugin("1.0.0"))
        with self.assertRaises(KeyError):
            self.registry.resolve("missing.plugin", "native")
        with self.assertRaises(KeyError):
            self.registry.resolve("dummy.deposition", "missing.variant")
        with self.assertRaises(KeyError):
            self.registry.resolve("dummy.deposition", "native", expected_plugin_version="9.9.9")
        with self.assertRaises(KeyError):
            self.registry.resolve("dummy.deposition", "native", expected_variant_version="9.9.9")
        with self.assertRaises(KeyError):
            self.registry.get("missing.plugin")

    def test_duplicate_registration_raises_value_error(self) -> None:
        self.registry.register(DummyPlugin("1.0.0"))
        with self.assertRaises(ValueError):
            self.registry.register(DummyPlugin("1.0.0"))
        with self.assertRaises(ValueError):
            self.registry.register(DummyPlugin("1.0.0", variants=[SurrogateDummyVariant()]))
        with self.assertRaises(ValueError):
            self.registry.register(DummyPlugin("2.0.0", variants=[NativeDummyVariant(), NativeDummyVariant()]))

    def test_list_descriptors_returns_latest_per_plugin(self) -> None:
        self.registry.register(DummyPlugin("1.0.0", label="Old deposition"))
        self.registry.register(DummyPlugin("2.0.0", label="New deposition", variants=[NativeV2Variant()]))
        self.registry.register(DummyPlugin("1.0.0", plugin_id="dummy.etch", label="Etch"))

        descriptors = self.registry.list_descriptors()
        by_id = {descriptor.id: descriptor for descriptor in descriptors}
        self.assertEqual(len(descriptors), 2)
        self.assertEqual(by_id["dummy.deposition"].version, "2.0.0")
        self.assertEqual(by_id["dummy.deposition"].label, "New deposition")
        self.assertEqual(by_id["dummy.etch"].version, "1.0.0")

    def test_implementation_hash_matches_variant_source(self) -> None:
        self.registry.register(DummyPlugin())
        resolved = self.registry.resolve("dummy.deposition", "native")
        expected = hashlib.sha256(inspect.getsource(NativeDummyVariant).encode("utf-8")).hexdigest()
        self.assertEqual(resolved.implementation_hash, expected)
        self.assertEqual(len(resolved.implementation_hash), 64)
        again = self.registry.resolve("dummy.deposition", "native")
        self.assertEqual(again.implementation_hash, resolved.implementation_hash)

    def test_explicit_implementation_hash_is_used(self) -> None:
        self.registry.register(DummyPlugin(variants=[ExplicitHashVariant()]))
        resolved = self.registry.resolve("dummy.deposition", "explicit")
        self.assertEqual(resolved.implementation_hash, "sha256:explicit")

    def test_params_model_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValidationError):
            DummyParams(thickness=0)
        with self.assertRaises(ValidationError):
            DummyParams(thickness=-1.0)
        params = DummyParams(thickness=0.5)
        self.assertEqual(params.material, "Si")

    def test_variant_validate_reports_issues(self) -> None:
        variant = NativeDummyVariant()
        self.assertEqual(variant.validate(_context(), DummyParams(thickness=1.0)), [])
        issues = variant.validate(_context(), DummyParams(thickness=100.0))
        self.assertEqual(
            issues,
            [Issue(path="thickness", message="thickness exceeds 100x the grid spacing", severity="warning")],
        )

    def test_dummy_variant_execute_returns_result(self) -> None:
        variant = NativeDummyVariant()
        state = ProcessState(
            ps=object(),
            domain=object(),
            dimension=2,
            grid_delta=0.5,
            materials={"Si": 0},
            seed=7,
        )
        self.assertIsNone(state.fields)
        state.fields = {"voxel_adapter": None}

        result = variant.execute(state, DummyParams(thickness=0.3), _context())
        self.assertIs(result.domain, state.domain)
        self.assertEqual(result.metadata, {"thickness": 0.3, "variant": "native"})
        self.assertEqual(result.outputs, ["dummy"])
        self.assertEqual(result.warnings, [])


if __name__ == "__main__":
    unittest.main()
