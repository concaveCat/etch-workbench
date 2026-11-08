"""Direct parity tests for the generic worker-side v2 bridge.

Each test builds two identical level-set domains with the installed
``viennaps`` module, runs the historical worker level-set path on one and
:func:`worker.process_models_v2.run_v2_step` on the other, and compares the v2
result through :func:`worker.process_models_v2.compare_domains`. The params come
from the plugin's own :func:`plugins.isotropic.parse_step` hook, so the bridge is
exercised exactly the way the shadow hook uses it.

No server, worker subprocess, or data directory is involved; the bridge's own
state store writes into a per-test directory under ``var/``.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import viennaps as ps

import worker.simulate as simulate
from plugins.isotropic import parse_step
from process_models.plugins import load_registry
from worker.process_models_v2 import (
    compare_domains,
    find_v2_process,
    material_order,
    run_v2_step,
)

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "var" / "test-worker-v2-bridge"
GRID_DELTA = 0.2
X_EXTENT = 4.0
VIENNAPS_VERSION = str(ps.__version__)


def plane_project(dimension: int = 2) -> dict[str, Any]:
    """Minimal schema-5 project with a plane and no steps."""
    domain = {"gridDelta": GRID_DELTA, "xExtent": X_EXTENT}
    if dimension == 2:
        domain["xBoundary"] = "reflective"
    else:
        domain["yExtent"] = X_EXTENT
        domain["boundaryXY"] = "reflective"
    return {
        "schemaVersion": 5,
        "name": f"v2 bridge {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [],
        "solver": {"timeStepRatio": 0.25, "raysPerPoint": 32, "cpuThreads": 1},
    }


def trench_project() -> dict[str, Any]:
    """Minimal schema-5 project with a masked trench."""
    project = plane_project(2)
    project["geometry"] = {
        "kind": "trench",
        "width": 1.6,
        "depth": 1.2,
        "taperAngle": 0.0,
        "maskHeight": 0.6,
        "maskTaperAngle": 0.0,
        "substrateMaterial": "Si",
        "maskMaterial": "Mask",
    }
    return project


def isotropic_step(
    operation: str = "etch",
    *,
    rate: float = 0.3,
    duration: float = 1.0,
    ratio: float | None = None,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": f"step_iso_{operation}",
        "name": "各向同性",
        "enabled": True,
        "operation": operation,
        "model": "isotropic",
        "rateMagnitude": rate,
        "duration": duration,
        "rateMode": "uniform",
    }
    if operation == "deposit":
        step["depositMaterial"] = "SiO2"
    if ratio is not None:
        step["isotropicTimeStepRatio"] = ratio
    return step


def bridge_ctx(
    project: dict[str, Any],
    run_dir: Path,
    *,
    engine_fingerprint: str | None = None,
) -> SimpleNamespace:
    """Minimal stand-in for the fields ``run_v2_step`` reads from a RunContext."""
    return SimpleNamespace(
        project=project,
        ps=ps,
        run_dir=Path(run_dir),
        ps_version=VIENNAPS_VERSION,
        native_engine_fingerprint=engine_fingerprint,
    )


def plane_domain(dimension: int = 2) -> Any:
    ps.setDimension(dimension)
    if dimension == 2:
        domain = ps.Domain(gridDelta=GRID_DELTA, xExtent=X_EXTENT, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    else:
        domain = ps.Domain(
            gridDelta=GRID_DELTA,
            xExtent=X_EXTENT,
            yExtent=X_EXTENT,
            boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
        )
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def trench_domain() -> Any:
    ps.setDimension(2)
    domain = ps.Domain(gridDelta=GRID_DELTA, xExtent=X_EXTENT, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakeTrench(
        domain=domain,
        trenchWidth=1.6,
        trenchDepth=1.2,
        trenchTaperAngle=0.0,
        maskHeight=0.6,
        maskTaperAngle=0.0,
        halfTrench=False,
        material=ps.Material.Si,
        maskMaterial=ps.Material.Mask,
    ).apply()
    return domain


def _old_reference(ps_module: Any, project: dict[str, Any], step: dict[str, Any], domain: Any) -> None:
    """Mirror the worker's isotropic level-set path in place."""
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(simulate.material_handle(ps_module, step["depositMaterial"]))
    rate, _signed = simulate.signed_rates(step)
    model = ps_module.IsotropicProcess(
        rate=rate,
        maskMaterials=[
            simulate.material_handle(ps_module, name) for name in simulate.protected_material_names(project, step)
        ],
    )
    process = ps_module.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(ps_module.FluxEngineType.CPU_DISK)
    advection, _actual = simulate.configure_advection(
        ps_module,
        project,
        time_step_ratio=step.get("isotropicTimeStepRatio"),
    )
    process.setParameters(advection)
    process.apply()


def _top(domain: Any, dimension: int) -> float:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    axis = 1 if dimension == 2 else 2
    return max(float(node[axis]) for node in mesh.getNodes())


class _BridgeTestBase(unittest.TestCase):
    """Per-test work directory; no tests live here so it is not collected."""

    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        self.work_dir = TEST_ROOT / self._testMethodName
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)


class MaterialOrderTests(_BridgeTestBase):
    def test_material_order_matches_simulate_material_map(self) -> None:
        for label, domain in (
            ("plane", plane_domain(2)),
            ("trench", trench_domain()),
        ):
            with self.subTest(geometry=label):
                self.assertEqual(list(material_order(domain)), simulate.material_map(domain))
                self.assertEqual(material_order(domain), tuple(simulate.material_map(domain)))

        deposited = plane_domain(2)
        deposited.duplicateTopLevelSet(ps.Material.SiO2)
        self.assertEqual(list(material_order(deposited)), simulate.material_map(deposited))
        self.assertEqual(material_order(deposited), (int(ps.Material.Si), int(ps.Material.SiO2)))


class FindProcessTests(_BridgeTestBase):
    def test_isotropic_uniform_step_matches_one_process(self) -> None:
        spec = find_v2_process(plane_project(2), isotropic_step("etch"))

        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.id, "isotropic")
        self.assertEqual(spec.default_variant, "native")
        self.assertIs(spec.parse_step, parse_step)

    def test_non_plugin_step_has_no_process(self) -> None:
        step = {
            "id": "step_directional",
            "name": "directional",
            "operation": "etch",
            "model": "directional",
            "direction": [0.0, -1.0, 0.0],
            "directionalRateMagnitude": 0.1,
            "isotropicRateMagnitude": 0.0,
            "duration": 1.0,
            "rateMode": "uniform",
        }

        self.assertIsNone(find_v2_process(plane_project(2), step))

    def test_material_rate_mode_is_claimed_by_the_isotropic_process(self) -> None:
        step = isotropic_step("etch", rate=0.0)
        step["rateMode"] = "material"
        step["materialRates"] = {"Si": 0.2}

        spec = find_v2_process(plane_project(2), step)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.id, "isotropic")
        params = parse_step(plane_project(2), step)
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.rateMode, "material")
        self.assertEqual(params.rateMagnitude, 0.0)
        self.assertEqual(params.materialRates, {"Si": 0.2})

    def test_find_v2_process_accepts_a_caller_registry(self) -> None:
        registry = load_registry()
        with mock.patch("worker.process_models_v2.load_registry") as loader:
            spec = find_v2_process(plane_project(2), isotropic_step("etch"), registry=registry)

        loader.assert_not_called()
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.id, "isotropic")


class ParseStepTests(_BridgeTestBase):
    def test_uniform_deposit_params(self) -> None:
        step = isotropic_step("deposit", rate=0.3, duration=1.0, ratio=0.25)
        params = parse_step(plane_project(2), step)
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.operation, "deposit")
        self.assertEqual(params.rateMode, "uniform")
        self.assertEqual(params.rateMagnitude, 0.3)
        self.assertEqual(params.duration, 1.0)
        self.assertEqual(params.depositMaterial, "SiO2")
        self.assertEqual(params.maskMaterials, [])
        self.assertEqual(params.materialRates, {})
        self.assertEqual(params.isotropicTimeStepRatio, 0.25)

        without_ratio = parse_step(plane_project(2), isotropic_step("etch"))
        self.assertIsNotNone(without_ratio)
        assert without_ratio is not None
        self.assertIsNone(without_ratio.isotropicTimeStepRatio)
        self.assertEqual(without_ratio.operation, "etch")

    def test_unsupported_steps_return_none(self) -> None:
        geometric = isotropic_step("etch")
        geometric["isotropicMethod"] = "geometric"
        directional = isotropic_step("etch")
        directional["model"] = "directional"
        unknown_operation = isotropic_step("etch")
        unknown_operation["operation"] = "melt"

        for label, step in (
            ("geometric", geometric),
            ("directional", directional),
            ("operation", unknown_operation),
        ):
            with self.subTest(step=label):
                self.assertIsNone(parse_step(plane_project(2), step))

    def test_missing_rate_fields_raise(self) -> None:
        step = isotropic_step("etch")
        del step["rateMagnitude"]
        with self.assertRaises(ValueError) as raised:
            parse_step(plane_project(2), step)
        self.assertIn("rateMagnitude", str(raised.exception))


class IsotropicParityTests(_BridgeTestBase):
    def _assert_parity(
        self,
        reference: Any,
        candidate: Any,
        dimension: int,
    ) -> dict[str, Any]:
        result = compare_domains(reference, candidate, dimension)
        self.assertTrue(result["matched"], result["reasons"])
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["oldDigest"], result["newDigest"])
        self.assertEqual(result["oldMaterialMap"], result["newMaterialMap"])
        self.assertEqual(result["oldMaterialMap"], simulate.material_map(candidate))
        self.assertGreater(result["oldPointCount"], 0)
        self.assertGreater(result["oldCellCount"], 0)
        self.assertEqual(result["oldPointCount"], result["newPointCount"])
        self.assertEqual(result["oldCellCount"], result["newCellCount"])
        return result

    def _run(self, project: dict[str, Any], step: dict[str, Any], domain: Any, **kwargs: Any):
        ctx = bridge_ctx(project, self.work_dir / f"run-{step['id']}", **kwargs)
        before = _top(domain, int(project["dimension"]))
        outcome = run_v2_step(ctx, step, domain)
        new_domain = outcome.state.geometry.handle
        self.assertIsNot(new_domain, domain)
        return outcome, new_domain, before

    def test_etch_matches_the_worker_level_set_path(self) -> None:
        project = plane_project(2)
        step = isotropic_step("etch", rate=0.3, duration=1.0, ratio=0.25)
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _old_reference(ps, project, step, reference)
        outcome, new_domain, before = self._run(project, step, candidate)

        self._assert_parity(reference, new_domain, 2)
        self.assertAlmostEqual(_top(new_domain, 2) - before, -0.3, delta=1e-6)
        # The bridge checkpoints before the mutating variant runs.
        self.assertAlmostEqual(_top(candidate, 2) - before, 0.0, delta=1e-12)

        self.assertIs(outcome.delta.geometry.handle, outcome.state.geometry.handle)
        self.assertEqual(outcome.delta.geometry.dimension, 2)
        self.assertEqual(outcome.delta.materials.order, material_order(new_domain))
        self.assertEqual(outcome.state.materials, outcome.delta.materials)
        self.assertEqual(outcome.metadata["nativeClass"], "ps.IsotropicProcess")
        self.assertEqual(outcome.metadata["operation"], "etch")
        self.assertEqual(outcome.metadata["timeStepRatio"], 0.25)

        identity = outcome.plan.identity_json()
        self.assertEqual(identity["processId"], "isotropic")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineFingerprint"], "worker-v2-plugin")

    def test_deposit_matches_the_worker_level_set_path(self) -> None:
        project = plane_project(2)
        step = isotropic_step("deposit", rate=0.3, duration=1.0)
        reference = plane_domain(2)

        _old_reference(ps, project, step, reference)
        outcome, new_domain, before = self._run(project, step, plane_domain(2), engine_fingerprint="worker-v2-test")

        self._assert_parity(reference, new_domain, 2)
        self.assertAlmostEqual(_top(new_domain, 2) - before, 0.3, delta=1e-6)
        self.assertIn(int(ps.Material.SiO2), simulate.material_map(new_domain))
        self.assertEqual(outcome.metadata["depositMaterial"], "SiO2")
        self.assertEqual(outcome.plan.identity_json()["engineFingerprint"], "worker-v2-test")

    def test_masked_trench_matches_the_worker_level_set_path(self) -> None:
        project = trench_project()
        step = isotropic_step("etch", rate=0.2, duration=0.5)
        reference = trench_domain()
        self.assertEqual(simulate.protected_material_names(project, step), ["Mask"])

        _old_reference(ps, project, step, reference)
        outcome, new_domain, _before = self._run(project, step, trench_domain())

        self._assert_parity(reference, new_domain, 2)
        self.assertEqual(outcome.metadata["maskMaterials"], ["Mask"])
        self.assertIn(int(ps.Material.Mask), simulate.material_map(new_domain))

    def test_three_dimensional_plane_smoke(self) -> None:
        project = plane_project(3)
        step = isotropic_step("etch", rate=0.2, duration=0.5)

        outcome, new_domain, before = self._run(project, step, plane_domain(3))

        self.assertAlmostEqual(_top(new_domain, 3) - before, -0.1, delta=1e-6)
        self.assertEqual(outcome.delta.geometry.dimension, 3)
        self.assertEqual(outcome.delta.materials.order, material_order(new_domain))
        self.assertEqual(outcome.state.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        surface = simulate.surface_state(new_domain, 3)
        self.assertGreater(surface["pointCount"], 0)
        self.assertEqual(material_order(new_domain), (int(ps.Material.Si),))


class RunArtifactsTests(_BridgeTestBase):
    def test_run_v2_step_writes_state_outputs_and_plan(self) -> None:
        project = plane_project(2)
        step = isotropic_step("etch", rate=0.2, duration=0.5)
        ctx = bridge_ctx(project, self.work_dir / "run")

        outcome = run_v2_step(ctx, step, plane_domain(2))

        # The store root is <run_dir>/v2/<step>; StateStore keeps its usual
        # ``steps/<step_id>/`` layout below it.
        root = ctx.run_dir / "v2" / step["id"]
        step_dir = root / "steps" / step["id"]
        for name in ("state.json", "outputs.json", "plan.json"):
            with self.subTest(file=name):
                self.assertTrue((step_dir / name).is_file(), sorted(path.name for path in root.rglob("*")))
        self.assertTrue((root / "flow.json").is_file())
        self.assertTrue((step_dir / "geometry.vpsd").is_file())

        state = json.loads((step_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["schemaVersion"], 1)
        self.assertEqual(state["stepId"], step["id"])
        self.assertEqual(state["geometry"]["ref"], "geometry.vpsd")
        self.assertEqual(state["geometry"]["dimension"], 2)
        self.assertEqual(state["materials"]["order"], list(material_order(outcome.state.geometry.handle)))

        plan = json.loads((step_dir / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan, outcome.plan.identity_json())

        outputs = json.loads((step_dir / "outputs.json").read_text(encoding="utf-8"))
        self.assertIsInstance(outputs, dict)

        flow = json.loads((root / "flow.json").read_text(encoding="utf-8"))
        self.assertEqual(flow["schemaVersion"], 1)
        self.assertEqual(flow["versions"], {"viennaps": VIENNAPS_VERSION})

    def test_run_v2_step_resolves_the_step_once_with_the_given_registry(self) -> None:
        project = plane_project(2)
        step = isotropic_step("etch", rate=0.2, duration=0.5)
        registry = load_registry()
        calls: list[str] = []
        original = registry.resolve_step

        def counting_resolve(project: dict[str, Any], step: dict[str, Any]):
            calls.append(step["id"])
            return original(project, step)

        registry.resolve_step = counting_resolve  # type: ignore[method-assign]
        ctx = bridge_ctx(project, self.work_dir / "run-once")

        outcome = run_v2_step(ctx, step, plane_domain(2), registry=registry)

        self.assertEqual(calls, [step["id"]])
        self.assertEqual(outcome.plan.process.id, "isotropic")


class CompareMismatchTests(_BridgeTestBase):
    def test_compare_reports_mismatch_without_raising(self) -> None:
        etched = plane_domain(2)
        untouched = plane_domain(2)
        _old_reference(ps, plane_project(2), isotropic_step("etch", rate=0.3, duration=1.0), etched)

        result = compare_domains(etched, untouched, 2)

        self.assertFalse(result["matched"])
        self.assertNotEqual(result["oldDigest"], result["newDigest"])
        self.assertEqual(result["oldMaterialMap"], result["newMaterialMap"])
        self.assertEqual(len(result["reasons"]), 1)
        self.assertIn("digest", result["reasons"][0])


if __name__ == "__main__":
    unittest.main()
