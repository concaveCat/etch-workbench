"""Tests for execution profiles, state backends, and the state contract."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import viennaps as ps
from pydantic import BaseModel

from process_models.v2.adapters import DefaultStateAdapter
from process_models.v2.contract import (
    Issue,
    ProcessContext,
    ProcessSpec,
    StateProfile,
    VariantCapabilities,
    VariantRegistration,
)
from process_models.v2.engine import (
    ComputePolicy,
    EngineCapabilities,
    EngineRequirement,
    EngineUnavailableError,
    ExecutionProfile,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.engines.viennaps import ViennaPSMaterialRegistry
from process_models.v2.materials import MaterialRegistryError, StaticMaterialRegistry
from process_models.v2.registry import ProcessRegistry
from process_models.v2.resolver import ExecutionRequest, ExecutionResolutionError, resolve_execution
from process_models.v2.state import LevelSetState, MaterialState, ProcessState, StateDelta, StepResult
from process_models.v2.state_backend import (
    HandleStateBackend,
    StateBackendError,
    StateCapabilities,
)

GRID_DELTA = 0.2
X_EXTENT = 4.0
SI = int(ps.Material.Si)


class DummyParams(BaseModel):
    amount: float = 1.0


class DummyVariant:
    variant_id = "dummy"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry"}),
        writes=frozenset({"geometry"}),
    )
    engine = EngineRequirement(
        kind="native",
        engine_id="dummy",
        profiles=(ExecutionProfile(backend="cpu", kernel="CPU_DISK"),),
    )
    implementation_hash = None

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        del ctx, params
        return []

    def execute(self, state: ProcessState, params: BaseModel, ctx: ProcessContext, engine: Any) -> StepResult:
        del params, ctx, engine
        return StepResult(delta=StateDelta(geometry=state.geometry))


def _plane() -> Any:
    ps.setDimension(2)
    ps.Length.setUnit("um")
    ps.setNumThreads(1)
    domain = ps.Domain(
        gridDelta=GRID_DELTA,
        xExtent=X_EXTENT,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def _state(domain: Any) -> ProcessState:
    return ProcessState(
        geometry=LevelSetState(handle=domain, dimension=2, grid_delta=GRID_DELTA),
        materials=MaterialState(order=(SI,), names={SI: "Si"}),
    )


def _context() -> ProcessContext:
    return ProcessContext(dimension=2, grid_delta=GRID_DELTA)


def _registry(variant: Any) -> ProcessRegistry:
    registry = ProcessRegistry()
    registry.register_process(
        ProcessSpec(
            id="dummy",
            version="1.0.0",
            label="Dummy",
            params_model=DummyParams,
            dimensions=(2, 3),
            operations=("test",),
        )
    )
    registry.register_variant(VariantRegistration(process_id="dummy", variant=variant))
    return registry


def _engine(
    *,
    backends: tuple[str, ...] = ("cpu",),
    kernels: tuple[str, ...] = ("CPU_DISK",),
    devices: tuple[str, ...] = (),
    state_backend: Any | None = None,
) -> StaticEngine:
    return StaticEngine(
        capabilities=EngineCapabilities(
            engine_id="dummy",
            version="1.0.0",
            kind="native",
            backends=backends,
            kernels=kernels,
            devices=devices,
        ),
        handle=object(),
        fingerprint="dummy-fp",
        state_backend=state_backend,
    )


def _provider(
    *,
    backends: tuple[str, ...] = ("cpu",),
    kernels: tuple[str, ...] = ("CPU_DISK",),
    devices: tuple[str, ...] = (),
    state_backend: Any | None = None,
) -> StaticEngineProvider:
    return StaticEngineProvider(
        [
            _engine(
                backends=backends,
                kernels=kernels,
                devices=devices,
                state_backend=state_backend,
            )
        ]
    )


class ExecutionProfileTest(unittest.TestCase):
    def test_profile_validation(self) -> None:
        self.assertEqual(
            ExecutionProfile(backend="cpu", kernel="CPU_DISK").identity(),
            {"backend": "cpu", "kernel": "CPU_DISK", "device": "", "strategy": ""},
        )
        with self.assertRaises(ValueError):
            ExecutionProfile(backend="any")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ExecutionProfile(backend="cpu", device="gpu-0")
        with self.assertRaises(ValueError):
            ExecutionProfile(backend="cpu", kernel=" ")

    def test_compute_policy_validation(self) -> None:
        self.assertEqual(ComputePolicy().backend, "auto")
        with self.assertRaises(ValueError):
            ComputePolicy(backend="cpu", device="gpu-0")
        with self.assertRaises(ValueError):
            ComputePolicy(backend="gpu", kernel=" ")

    def test_declared_profiles_cannot_pin_devices_or_contradict_backend(self) -> None:
        with self.assertRaises(ValueError):
            EngineRequirement(
                kind="native",
                engine_id="dummy",
                profiles=(ExecutionProfile(backend="gpu", kernel="GPU_DISK", device="gpu-0"),),
            )
        with self.assertRaises(ValueError):
            EngineRequirement(
                kind="native",
                engine_id="dummy",
                backend="cpu",
                profiles=(ExecutionProfile(backend="gpu", kernel="GPU_DISK"),),
            )


class ProfileResolutionTest(unittest.TestCase):
    def test_cpu_profile_matches_explicit_policy(self) -> None:
        engine = _engine()
        lease = engine.acquire(
            DummyVariant.engine,
            ComputePolicy(backend="cpu", kernel="CPU_DISK"),
        )
        self.assertEqual(lease.profile, ExecutionProfile(backend="cpu", kernel="CPU_DISK"))

    def test_gpu_policy_is_rejected_on_cpu_engine(self) -> None:
        engine = _engine()
        with self.assertRaises(EngineUnavailableError):
            engine.acquire(DummyVariant.engine, ComputePolicy(backend="gpu", kernel="GPU_DISK"))

    def test_kernel_mismatch_is_rejected(self) -> None:
        engine = _engine()
        with self.assertRaises(EngineUnavailableError):
            engine.acquire(DummyVariant.engine, ComputePolicy(backend="cpu", kernel="CPU_TRIANGLE"))

    def test_gpu_device_is_filled_and_unknown_device_is_rejected(self) -> None:
        variant = type(
            "GpuVariant",
            (DummyVariant,),
            {
                "engine": EngineRequirement(
                    kind="native",
                    engine_id="dummy",
                    profiles=(ExecutionProfile(backend="gpu", kernel="GPU_DISK"),),
                )
            },
        )
        provider = _provider(
            backends=("gpu",),
            kernels=("GPU_DISK",),
            devices=("gpu-0", "gpu-1"),
        )
        lease = provider.acquire(
            variant.engine,
            ComputePolicy(backend="gpu", kernel="GPU_DISK", device="gpu-1"),
        )
        self.assertEqual(lease.profile.device, "gpu-1")
        with self.assertRaises(EngineUnavailableError):
            provider.acquire(
                variant.engine,
                ComputePolicy(backend="gpu", kernel="GPU_DISK", device="missing"),
            )

    def test_legacy_requirement_still_selects_first_backend(self) -> None:
        engine = _engine(backends=("cpu", "gpu"), kernels=())
        lease = engine.acquire(EngineRequirement(kind="native", engine_id="dummy"))
        self.assertEqual(lease.backend, "cpu")
        gpu = engine.acquire(EngineRequirement(kind="native", engine_id="dummy", backend="gpu"))
        self.assertEqual(gpu.backend, "gpu")


class MaterialCreationGuardTest(unittest.TestCase):
    def test_new_materials_require_an_explicit_profile_flag(self) -> None:
        state = _state(_plane())
        delta = StateDelta(
            materials=MaterialState(
                order=(SI, 999),
                names={SI: "Si", 999: "SiO2"},
            )
        )
        blocked = VariantCapabilities(
            dimensions=(2,),
            reads=frozenset({"geometry"}),
            writes=frozenset({"geometry", "materials"}),
            state_profile=StateProfile(allow_new_materials=False),
        )
        with self.assertRaises(ExecutionResolutionError) as caught:
            DefaultStateAdapter().commit(state, delta, blocked)
        self.assertIn("SiO2", str(caught.exception))

        allowed = VariantCapabilities(
            dimensions=(2,),
            reads=frozenset({"geometry"}),
            writes=frozenset({"geometry", "materials"}),
            state_profile=StateProfile(allow_new_materials=True),
        )
        committed = DefaultStateAdapter().commit(state, delta, allowed)
        self.assertEqual(committed.materials.order, (SI, 999))


class InstalledPluginProfileTest(unittest.TestCase):
    def test_installed_plugins_declare_their_cpu_kernel(self) -> None:
        from plugins.isotropic import IsotropicNativeVariant
        from plugins.sf6o2 import SF6O2NativeVariant

        cpu = ExecutionProfile(backend="cpu", kernel="CPU_DISK")
        self.assertEqual(IsotropicNativeVariant.engine.profiles, (cpu,))
        self.assertEqual(
            SF6O2NativeVariant.engine.profiles,
            (
                cpu,
                ExecutionProfile(backend="cpu", kernel="CPU_TRIANGLE"),
                ExecutionProfile(backend="gpu", kernel="GPU_DISK"),
                ExecutionProfile(backend="gpu", kernel="GPU_TRIANGLE"),
            ),
        )
        self.assertTrue(IsotropicNativeVariant.capabilities.state_profile.allow_new_materials)
        self.assertFalse(SF6O2NativeVariant.capabilities.state_profile.allow_new_materials)


class StateProfileTest(unittest.TestCase):
    def test_profile_validation(self) -> None:
        self.assertEqual(StateProfile().conversions, ())
        with self.assertRaises(ValueError):
            StateProfile(conversions=("",))
        with self.assertRaises(ValueError):
            StateProfile(required_materials=("",))
        with self.assertRaises(ValueError):
            StateProfile(persistence=frozenset({"unknown"}))  # type: ignore[arg-type]


class StateContractTest(unittest.TestCase):
    def _variant(self, profile: StateProfile) -> Any:
        return type(
            "ProfiledVariant",
            (DummyVariant,),
            {
                "capabilities": VariantCapabilities(
                    dimensions=(2, 3),
                    reads=frozenset({"geometry"}),
                    writes=frozenset({"geometry"}),
                    state_profile=profile,
                )
            },
        )()

    def _resolve(self, variant: Any, *, backend: Any | None = None):
        return resolve_execution(
            ExecutionRequest(
                process_id="dummy",
                variant_id="dummy",
                params=DummyParams(),
                context=_context(),
                state=_state(_plane()),
                compute=ComputePolicy(backend="cpu", kernel="CPU_DISK"),
            ),
            _registry(variant),
            _provider(state_backend=backend),
        )

    def test_plan_identity_records_profile_and_contract(self) -> None:
        plan = self._resolve(DummyVariant())
        identity = plan.identity()
        self.assertEqual(identity["engine_backend"], "cpu")
        self.assertEqual(identity["engine_kernel"], "CPU_DISK")
        self.assertEqual(identity["state_backend_id"], "handle-domain")
        self.assertIn("state_contract", identity)
        self.assertEqual(plan.state_contract.dimension, 2)
        json.dumps(identity)

    def test_gpu_profile_changes_the_plan_identity(self) -> None:
        variant = type(
            "GpuVariant",
            (DummyVariant,),
            {
                "engine": EngineRequirement(
                    kind="native",
                    engine_id="dummy",
                    profiles=(ExecutionProfile(backend="gpu", kernel="GPU_DISK"),),
                )
            },
        )()
        plan = resolve_execution(
            ExecutionRequest(
                process_id="dummy",
                variant_id="dummy",
                params=DummyParams(),
                context=_context(),
                state=_state(_plane()),
                compute=ComputePolicy(backend="gpu", kernel="GPU_DISK"),
            ),
            _registry(variant),
            _provider(backends=("gpu",), kernels=("GPU_DISK",)),
        )
        self.assertEqual(plan.identity()["engine_backend"], "gpu")
        self.assertEqual(plan.identity()["engine_kernel"], "GPU_DISK")

    def test_missing_required_material_fails_closed(self) -> None:
        variant = self._variant(StateProfile(required_materials=("Missing",)))
        backend = HandleStateBackend(
            object(),
            material_registry=StaticMaterialRegistry({"Si": SI}),
        )
        with self.assertRaises(ExecutionResolutionError) as caught:
            self._resolve(variant, backend=backend)
        self.assertIn("Missing", str(caught.exception))

    def test_required_material_passes_with_registry(self) -> None:
        variant = self._variant(StateProfile(required_materials=("Si",)))
        backend = HandleStateBackend(
            object(),
            material_registry=StaticMaterialRegistry({"Si": SI}),
        )
        plan = self._resolve(variant, backend=backend)
        self.assertEqual(plan.state_contract.required_materials, ("Si",))

    def test_unsupported_conversion_fails_closed(self) -> None:
        variant = self._variant(StateProfile(conversions=("voxels<-geometry:height-field-v1",)))
        with self.assertRaises(ExecutionResolutionError) as caught:
            self._resolve(variant)
        self.assertIn("conversion", str(caught.exception).lower())

    def test_unsupported_persistence_fails_closed(self) -> None:
        variant = self._variant(StateProfile(persistence=frozenset({"voxels"})))
        backend = HandleStateBackend(object())
        with self.assertRaises(ExecutionResolutionError) as caught:
            self._resolve(variant, backend=backend)
        self.assertIn("persist", str(caught.exception).lower())


class StateBackendTest(unittest.TestCase):
    def test_checkpoint_validate_write_and_load(self) -> None:
        ps.setDimension(2)
        ps.Length.setUnit("um")
        ps.setNumThreads(1)
        domain = _plane()
        state = _state(domain)
        backend = HandleStateBackend(ps, id="viennaps", version=str(ps.__version__))
        backend.validate(state)

        copied = backend.checkpoint(state)
        self.assertIsNot(copied.geometry.handle, domain)
        self.assertEqual(len(domain.getLevelSets()), 1)

        with tempfile.TemporaryDirectory() as temporary:
            reference = backend.write(state, Path(temporary))
            self.assertIsNotNone(reference)
            assert reference is not None
            self.assertEqual(reference.path, "geometry.vpsd")
            self.assertTrue((Path(temporary) / reference.path).is_file())

            loader = HandleStateBackend(
                ps,
                id="viennaps",
                version=str(ps.__version__),
                domain_factory=lambda ref: ps.Domain(
                    gridDelta=ref.grid_delta,
                    xExtent=X_EXTENT,
                    boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
                ),
            )
            loaded = loader.load(reference, Path(temporary))
            self.assertEqual(len(loaded.geometry.handle.getLevelSets()), 1)

    def test_validate_rejects_material_order_mismatch(self) -> None:
        backend = HandleStateBackend(ps, id="viennaps", version=str(ps.__version__))
        state = _state(_plane())
        mismatched = ProcessState(
            geometry=state.geometry,
            materials=MaterialState(order=(999,), names={999: "Bogus"}),
        )
        with self.assertRaises(StateBackendError):
            backend.validate(mismatched)

    def test_state_capabilities_validation(self) -> None:
        with self.assertRaises(ValueError):
            StateCapabilities(persistence=frozenset({"voxels"}))
        with self.assertRaises(ValueError):
            StateCapabilities(representations=frozenset({"unknown"}))  # type: ignore[arg-type]

    def test_material_registries(self) -> None:
        static = StaticMaterialRegistry({"Si": SI, "SiO2": SI + 1})
        self.assertEqual(static.id_for("Si"), SI)
        self.assertEqual(static.name_for(SI), "Si")
        with self.assertRaises(MaterialRegistryError):
            static.id_for("Missing")
        with self.assertRaises(ValueError):
            StaticMaterialRegistry({"Si": SI, "Alias": SI})

        native = ViennaPSMaterialRegistry(ps)
        self.assertEqual(native.id_for("Si"), SI)
        self.assertIsNotNone(native.name_for(SI))
        with self.assertRaises(MaterialRegistryError):
            native.id_for("DefinitelyMissingMaterial")


if __name__ == "__main__":
    unittest.main()
