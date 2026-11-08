"""Conformance-harness tests for v2 plugin variants.

These tests use dummy variants, a mutable dummy handle, and a static dummy
engine. They do not import ViennaPS, execute native code, or touch the data
directory; the harness itself has no native dependency.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel

from process_models.v2 import (
    EngineCapabilities,
    EngineRequirement,
    LevelSetState,
    MaterialState,
    OutputSet,
    ProcessContext,
    ProcessRegistry,
    ProcessSpec,
    ProcessState,
    StateDelta,
    StaticEngine,
    StaticEngineProvider,
    StepResult,
    VariantCapabilities,
    VariantRegistration,
)
from process_models.v2.conformance import ConformanceReport, check_variant
from process_models.v2.contract import EngineVariant, Issue


class DummyParams(BaseModel):
    value: int = 1


class OtherParams(BaseModel):
    other: int = 0


class DummyHandle:
    """A mutable stand-in for an opaque native level-set handle."""

    def __init__(self, label: str = "handle") -> None:
        self.label = label
        self.touched = 0

    def touch(self) -> None:
        self.touched += 1

    def __repr__(self) -> str:
        # The fingerprint check relies on content showing up here.
        return f"DummyHandle(label={self.label!r}, touched={self.touched})"


class PassingVariant(EngineVariant):
    """A contract-clean variant that only reports outputs."""

    variant_id = "passing"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2,),
        reads=frozenset({"geometry"}),
        writes=frozenset(),
        output_kinds=("metrology",),
    )
    engine = EngineRequirement(kind="native", engine_id="conformance", min_version="1.0.0")
    implementation_hash = "a" * 64

    def __init__(self) -> None:
        self.seen_states: list[ProcessState] = []

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        return []

    def execute(self, state: ProcessState, params: BaseModel, ctx: ProcessContext, engine: Any) -> StepResult:
        self.seen_states.append(state)
        return StepResult(
            delta=StateDelta(),
            outputs=OutputSet(),
            metadata={"value": params.value, "engine": engine.engine_id},
            warnings=["passing variant warning"],
        )


class MaterialsWritingVariant(PassingVariant):
    """Commits a new canonical material state."""

    variant_id = "writes-materials"
    capabilities = VariantCapabilities(
        dimensions=(2,),
        reads=frozenset({"geometry"}),
        writes=frozenset({"materials"}),
    )

    def execute(self, state: ProcessState, params: BaseModel, ctx: ProcessContext, engine: Any) -> StepResult:
        self.seen_states.append(state)
        return StepResult(
            delta=StateDelta(materials=MaterialState(order=(10, 20), names={10: "Si", 20: "SiO2"})),
            outputs=OutputSet(),
        )


class LegacyDeltaVariant(PassingVariant):
    """Returns the pre-v2.2 ``StateDelta`` instead of a ``StepResult``."""

    variant_id = "legacy-delta"

    def execute(self, state: ProcessState, params: BaseModel, ctx: ProcessContext, engine: Any) -> Any:
        return StateDelta(geometry=state.geometry)


class InvalidVariant(PassingVariant):
    """``validate`` reports an error, so the harness must not execute it."""

    variant_id = "invalid"

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        return [Issue(path="value", message="must be positive", severity="error")]


class WarningVariant(PassingVariant):
    """Advisory findings are not conformance failures."""

    variant_id = "warning"

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        return [Issue(path="value", message="advisory only", severity="warning")]


class CountingVariant(PassingVariant):
    """Metadata changes between runs, so strict determinism must fail."""

    variant_id = "counting"

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def execute(self, state: ProcessState, params: BaseModel, ctx: ProcessContext, engine: Any) -> StepResult:
        self.calls += 1
        self.seen_states.append(state)
        return StepResult(delta=StateDelta(), outputs=OutputSet(), metadata={"call": self.calls})


class HandleTouchingVariant(PassingVariant):
    """Mutates the input handle in place without declaring ``mutates_input``."""

    variant_id = "touches-handle"

    def execute(self, state: ProcessState, params: BaseModel, ctx: ProcessContext, engine: Any) -> StepResult:
        self.seen_states.append(state)
        state.geometry.handle.touch()
        return StepResult(delta=StateDelta(), outputs=OutputSet())


@dataclass(frozen=True)
class Fixture:
    registry: ProcessRegistry
    provider: StaticEngineProvider
    spec: ProcessSpec
    context: ProcessContext


def _fixture(*variants: EngineVariant) -> Fixture:
    spec = ProcessSpec(
        id="conformance",
        version="1.0.0",
        label="Conformance dummy",
        params_model=DummyParams,
        dimensions=(2,),
        operations=("test",),
    )
    registry = ProcessRegistry()
    registry.register_process(spec)
    for variant in variants:
        registry.register_variant(VariantRegistration(process_id=spec.id, variant=variant))

    engine = StaticEngine(
        capabilities=EngineCapabilities(
            engine_id="conformance",
            version="1.0.0",
            kind="native",
            backends=("cpu",),
        ),
        handle=object(),
        fingerprint="conformance-fingerprint",
    )
    return Fixture(
        registry=registry,
        provider=StaticEngineProvider([engine]),
        spec=spec,
        context=ProcessContext(dimension=2, grid_delta=0.1),
    )


def _state_factory(handle: DummyHandle | None = None) -> Callable[[], ProcessState]:
    """Return a factory that builds a fresh state, optionally sharing a handle."""

    def factory() -> ProcessState:
        return ProcessState(
            geometry=LevelSetState(
                handle=handle if handle is not None else DummyHandle(),
                dimension=2,
                grid_delta=0.1,
            ),
            materials=MaterialState(order=(10,), names={10: "Si"}),
        )

    return factory


def _check(
    fixture: Fixture,
    variant_id: str,
    *,
    params: BaseModel | None = None,
    state_factory: Callable[[], ProcessState] | None = None,
    digest: Any = None,
) -> ConformanceReport:
    return check_variant(
        fixture.registry,
        fixture.provider,
        fixture.spec.id,
        variant_id,
        params if params is not None else DummyParams(),
        fixture.context,
        state_factory if state_factory is not None else _state_factory(),
        digest=digest,
    )


class ConformanceHarnessTest(unittest.TestCase):
    def test_passing_variant_passes_and_records_details(self) -> None:
        variant = PassingVariant()
        report = _check(_fixture(variant), variant.variant_id)

        self.assertIsInstance(report, ConformanceReport)
        self.assertTrue(report.passed, report.issues)
        self.assertEqual(report.issues, [])
        self.assertEqual(report.process_id, "conformance")
        self.assertEqual(report.variant_id, "passing")

        identity = report.details["plan_identity"]
        self.assertEqual(identity["process_id"], "conformance")
        self.assertEqual(identity["variant_id"], "passing")
        self.assertEqual(identity["engine_id"], "conformance")
        self.assertEqual(identity["adapter"], "default-v2")

        self.assertEqual(report.details["output_counts"], {"metrology": 0, "artifacts": 0})
        self.assertEqual(report.details["state"]["materials"]["order"], [10])
        self.assertNotIn("material_order", report.details["state"]["geometry"])
        self.assertNotIn("materials", report.details["delta"])
        self.assertIn("passing variant warning", report.details["warnings"])
        self.assertTrue(report.details["determinism"]["checked"])
        self.assertEqual(report.details["determinism"]["compared_with"], "structure")
        self.assertEqual(report.details["determinism"]["differences"], [])
        # Two fresh runs were executed, as the strict-determinism check requires.
        self.assertEqual(len(variant.seen_states), 2)
        self.assertIsNot(variant.seen_states[0], variant.seen_states[1])

    def test_materials_delta_is_summarized_from_canonical_state(self) -> None:
        variant = MaterialsWritingVariant()
        report = _check(_fixture(variant), variant.variant_id)

        self.assertTrue(report.passed, report.issues)
        self.assertEqual(report.details["state"]["materials"]["order"], [10, 20])
        self.assertEqual(report.details["state"]["materials"]["names"], {"10": "Si", "20": "SiO2"})
        self.assertEqual(report.details["delta"]["materials"]["order"], [10, 20])
        self.assertNotIn("material_order", report.details["delta"]["materials"])
        self.assertNotIn("geometry", report.details["delta"])

    def test_legacy_state_delta_return_fails_with_clear_issue(self) -> None:
        variant = LegacyDeltaVariant()
        report = _check(_fixture(variant), variant.variant_id)

        self.assertFalse(report.passed)
        self.assertTrue(report.issues)
        joined = " ".join(report.issues)
        self.assertIn("StateDelta", joined)
        self.assertIn("StepResult", joined)
        self.assertIn("execution failed", joined)

    def test_validation_error_fails_before_execution(self) -> None:
        variant = InvalidVariant()
        report = _check(_fixture(variant), variant.variant_id)

        self.assertFalse(report.passed)
        self.assertTrue(any("must be positive" in issue for issue in report.issues))
        self.assertEqual(report.details["validation"]["errors"], 1)
        self.assertEqual(variant.seen_states, [])
        self.assertIsNone(report.details["plan_identity"])

    def test_validation_warning_is_recorded_but_does_not_fail(self) -> None:
        variant = WarningVariant()
        report = _check(_fixture(variant), variant.variant_id)

        self.assertTrue(report.passed, report.issues)
        self.assertEqual(report.issues, [])
        self.assertIn("advisory only", " ".join(report.details["warnings"]))
        self.assertEqual(report.details["validation"]["warnings"], 1)

    def test_non_mutating_variant_preserves_input_handle_identity(self) -> None:
        variant = PassingVariant()
        handle = DummyHandle()
        report = _check(
            _fixture(variant),
            variant.variant_id,
            state_factory=_state_factory(handle),
        )

        self.assertTrue(report.passed, report.issues)
        self.assertEqual(handle.touched, 0)
        self.assertEqual(len(variant.seen_states), 2)
        for state in variant.seen_states:
            self.assertIs(state.geometry.handle, handle)

    def test_strict_determinism_with_equal_digest_passes(self) -> None:
        variant = PassingVariant()
        report = _check(
            _fixture(variant),
            variant.variant_id,
            digest=lambda result, state: (result.metadata["value"], state.geometry.handle.label),
        )

        self.assertTrue(report.passed, report.issues)
        self.assertEqual(report.details["determinism"]["compared_with"], "digest")
        self.assertEqual(report.details["determinism"]["differences"], [])

    def test_strict_determinism_with_unequal_digest_fails(self) -> None:
        variant = CountingVariant()
        report = _check(
            _fixture(variant),
            variant.variant_id,
            digest=lambda result, state: result.metadata["call"],
        )

        self.assertFalse(report.passed)
        self.assertTrue(any("digest differs" in issue for issue in report.issues))
        self.assertEqual(report.details["determinism"]["compared_with"], "digest")
        self.assertTrue(report.details["determinism"]["differences"])

    def test_strict_determinism_without_digest_compares_structure(self) -> None:
        variant = CountingVariant()
        report = _check(_fixture(variant), variant.variant_id)

        self.assertFalse(report.passed)
        self.assertTrue(any("metadata differs" in issue for issue in report.issues))
        self.assertEqual(report.details["determinism"]["compared_with"], "structure")

    def test_mutated_input_handle_without_flag_fails(self) -> None:
        variant = HandleTouchingVariant()
        handle = DummyHandle()
        report = _check(
            _fixture(variant),
            variant.variant_id,
            state_factory=_state_factory(handle),
        )

        self.assertFalse(report.passed)
        joined = " ".join(report.issues)
        self.assertIn("mutates_input", joined)
        self.assertIn("geometry.handle", joined)

    def test_unknown_variant_id_fails(self) -> None:
        fixture = _fixture(PassingVariant())
        report = _check(fixture, "missing")

        self.assertFalse(report.passed)
        self.assertTrue(any("registry does not resolve" in issue for issue in report.issues))

    def test_wrong_params_type_fails_before_execution(self) -> None:
        variant = PassingVariant()
        report = _check(_fixture(variant), variant.variant_id, params=OtherParams())

        self.assertFalse(report.passed)
        self.assertTrue(any("DummyParams" in issue for issue in report.issues))
        self.assertEqual(variant.seen_states, [])


if __name__ == "__main__":
    unittest.main()
