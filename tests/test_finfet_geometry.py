"""Parity for the geometry operations introduced by the FinFET fixture.

Each case runs the same raw step through the legacy worker geometry path and
through the v2 ``GeometryEngine`` and compares the exposed surface digest and
the material order. The native reference is the legacy path, which these tests
also keep working for the new operations.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import unittest
from pathlib import Path
from typing import Any

import viennaps as ps

from process_models.v2.contract import ProcessContext
from process_models.v2.engine import EngineCapabilities, StaticEngine, StaticEngineProvider
from process_models.v2.executor import FlowExecutor, FlowStep
from process_models.v2.geometry import register_geometry
from process_models.v2.registry import ProcessRegistry
from process_models.v2.state import LevelSetState, MaterialState, ProcessState
from process_models.v2.state_backend import HandleStateBackend
from process_models.v2.storage import StateStore
from worker.process_models_v2 import compare_domains

ROOT = Path(__file__).resolve().parents[1]
GRID = 0.05


def _domain() -> Any:
    ps.setDimension(3)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    return ps.Domain(GRID, 1.0, 1.0, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)


def _plane(extra: tuple[str, ...] = ()) -> Any:
    domain = _domain()
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    for index, name in enumerate(extra):
        ps.MakePlane(domain, height=0.2 * (index + 1), material=getattr(ps.Material, name),
                     addToExisting=True).apply()
    return domain


def _project() -> dict[str, Any]:
    return {
        "schemaVersion": 7,
        "dimension": 3,
        "domain": {"gridDelta": GRID, "xExtent": 1.0, "yExtent": 1.0,
                   "boundaryXY": "reflective"},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "solver": {"timeStepRatio": 0.25, "advection": {}},
    }


def _order(domain: Any) -> list[int]:
    mapping = domain.getMaterialMap()
    return [int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size())]


def _state(domain: Any) -> ProcessState:
    order = tuple(_order(domain))
    names = {
        int(ps.Material.Si): "Si",
        int(ps.Material.Mask): "Mask",
        int(ps.Material.SiO2): "SiO2",
        int(ps.Material.Metal): "Metal",
    }
    return ProcessState(
        geometry=LevelSetState(handle=domain, dimension=3, grid_delta=GRID),
        materials=MaterialState(order=order, names={key: value for key, value in names.items() if key in order}),
    )


def _provider() -> StaticEngineProvider:
    return StaticEngineProvider(
        [
            StaticEngine(
                capabilities=EngineCapabilities(
                    engine_id="viennaps", version=str(ps.__version__), kind="native",
                    backends=("cpu",), kernels=("CPU_DISK",),
                ),
                handle=ps,
                fingerprint="finfet-geometry-test",
                state_backend=HandleStateBackend(ps, id="viennaps", version=str(ps.__version__)),
            )
        ]
    )


def _surface_digest(domain: Any) -> str:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    points = sorted((round(float(point[0]), 9), round(float(point[1]), 9), round(float(point[2]), 9))
                    for point in mesh.getNodes())
    digest = hashlib.sha256()
    for point in points:
        digest.update(struct.pack("<ddd", *point))
    return digest.hexdigest()


class FinFETGeometryParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work_dir = ROOT / "var" / f"test-finfet-geometry-{self._testMethodName}"
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)
        self.registry = ProcessRegistry()
        register_geometry(self.registry)
        self.project = _project()

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _run_v2(self, raw: dict[str, Any], base: Any) -> Any:
        spec, params = self.registry.resolve_step(self.project, raw)
        return (
            FlowExecutor(self.registry, _provider(), StateStore(self.work_dir / raw["id"]))
            .run(
                _state(ps.Domain(base)),
                [FlowStep(step_id=raw["id"], process_id=spec.id, variant_id="native",
                          params=params,
                          context=ProcessContext(dimension=3, grid_delta=GRID,
                                                 project=self.project, step=raw))],
            )
            .outcomes[-1]
        )

    @staticmethod
    def _run_legacy(raw: dict[str, Any], base: Any) -> Any:
        from worker import simulate

        domain = ps.Domain(base)
        simulate.apply_geometry_operation(ps, domain, _project(), raw, run_dir=ROOT / "var")
        return domain

    def _compare(self, raw: dict[str, Any], base: Any) -> tuple[Any, Any]:
        reference = self._run_legacy(raw, base)
        outcome = self._run_v2(raw, base)
        actual = outcome.state.geometry.handle
        comparison = compare_domains(reference, actual, 3)
        self.assertTrue(comparison["matched"], comparison["reasons"])
        self.assertEqual(_order(actual), _order(reference))
        self.assertEqual(_surface_digest(actual), _surface_digest(reference))
        return reference, actual

    def test_insert_box_creates_a_new_material_level_set(self) -> None:
        raw = {"id": "step_insert_box_01", "name": "mask", "enabled": True,
               "model": "insertBox", "min": [0.2, -0.1, 0.09],
               "max": [0.8, 1.1, 0.2], "material": "Mask"}
        reference, actual = self._compare(raw, _plane())
        self.assertEqual(_order(reference), [int(ps.Material.Si), int(ps.Material.Mask)])
        self.assertEqual(_order(actual), _order(reference))
        self.assertEqual(len(actual.getLevelSets()), 2)

    def test_remove_top_level_set_keeps_the_remaining_order(self) -> None:
        base = _plane(("SiO2", "Mask"))
        raw = {"id": "step_remove_top_01", "name": "strip", "enabled": True,
               "model": "removeTopLevelSet"}
        reference, actual = self._compare(raw, base)
        self.assertEqual(_order(reference), [int(ps.Material.Si), int(ps.Material.SiO2)])
        self.assertEqual(_order(actual), _order(reference))

    def test_particle_deposit_material_duplicates_the_top_level_set(self) -> None:
        raw = {"id": "step_shape_dep_01", "name": "metal", "enabled": True,
               "model": "particleShape", "operation": "deposit", "shape": "sphere",
               "radius": 0.1, "depositMaterial": "Metal"}
        reference, actual = self._compare(raw, _plane())
        self.assertEqual(_order(reference), [int(ps.Material.Si), int(ps.Material.Metal)])
        self.assertEqual(_order(actual), _order(reference))

    def test_particle_explicit_mask_protects_the_named_material(self) -> None:
        base = _plane()
        from worker import simulate

        simulate.apply_geometry_operation(
            ps, base, self.project,
            {"id": "step_insert_box_02", "name": "mask", "enabled": True,
             "model": "insertBox", "min": [0.2, -0.1, 0.09],
             "max": [0.8, 1.1, 0.2], "material": "Mask"},
            run_dir=ROOT / "var")
        raw = {"id": "step_shape_mask_01", "name": "etch", "enabled": True,
               "model": "particleShape", "operation": "etch", "shape": "box",
               "halfAxes": [GRID, GRID, 0.2], "maskMaterials": ["Mask"]}
        self._compare(raw, base)

    def test_particle_all_except_mask_matches_get_materials_in_domain(self) -> None:
        base = _plane(("SiO2", "Mask"))
        raw = {"id": "step_shape_mask_02", "name": "spacer etch", "enabled": True,
               "model": "particleShape", "operation": "etch", "shape": "box",
               "halfAxes": [GRID, GRID, 0.2], "maskMaterials": ["Mask"],
               "maskMode": "allExcept"}
        self._compare(raw, base)


if __name__ == "__main__":
    unittest.main()
