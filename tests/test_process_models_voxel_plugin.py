"""Tests for the experimental voxel-probe surrogate plugin.

The plugin is a proof that the per-step level-set <-> voxel adapter can be
driven from the process-model contract. These tests build one small 2D plane,
round-trip it, and erode a layer. No server and no data directory are used.
"""

from __future__ import annotations

import hashlib
import inspect
import unittest

import numpy as np
import viennaps as ps
from pydantic import ValidationError

from process_models import ProcessContext, ProcessModelRegistry, ProcessState
from process_models.surrogate import VoxelProbeParams, VoxelProbePlugin, VoxelProbeVariant

GRID_DELTA = 0.1
X_EXTENT = 1.0


def _plane_domain():
    ps.setDimension(2)
    domain = ps.Domain(
        gridDelta=GRID_DELTA,
        xExtent=X_EXTENT,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def _context(dimension: int = 2) -> ProcessContext:
    return ProcessContext(dimension=dimension, grid_delta=GRID_DELTA, materials={"Si": 0})


def _state(domain) -> ProcessState:
    return ProcessState(
        ps=ps,
        domain=domain,
        dimension=2,
        grid_delta=GRID_DELTA,
        materials={"Si": 0},
        seed=0,
    )


def _surface_points(domain) -> np.ndarray:
    mesh = domain.getSurfaceMesh()
    points = [[float(value) for value in node] for node in mesh.getNodes()]
    if not points:
        return np.zeros((0, 3), dtype=float)
    return np.asarray(points, dtype=float)


def _sorted_surface_points(domain) -> np.ndarray:
    points = _surface_points(domain)
    if points.size == 0:
        return points
    return points[np.lexsort((points[:, 0], points[:, 1], points[:, 2]))]


def _material_ids(domain) -> list[int]:
    material_map = domain.getMaterialMap()
    return [int(material_map.getMaterialIdAtIdx(index)) for index in range(material_map.size())]


def _surface_top(domain, dimension: int = 2) -> float:
    points = _surface_points(domain)
    if points.size == 0:
        raise AssertionError("domain has no surface nodes")
    vertical = 1 if dimension == 2 else 2
    return float(np.max(points[:, vertical]))


class VoxelProbePluginTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = VoxelProbePlugin()
        self.registry = ProcessModelRegistry()
        self.registry.register(self.plugin)

    def _variant(self) -> VoxelProbeVariant:
        return self.registry.resolve("voxel_probe", "voxel_probe").variant

    def test_registry_resolves_plugin_and_variant_identity(self) -> None:
        self.assertIs(self.registry.get("voxel_probe"), self.plugin)

        resolved = self.registry.resolve("voxel_probe", "voxel_probe")
        self.assertEqual(resolved.plugin_id, "voxel_probe")
        self.assertEqual(resolved.plugin_version, "0.1.0")
        self.assertEqual(resolved.variant_id, "voxel_probe")
        self.assertEqual(resolved.variant_version, "0.1.0")
        self.assertIsInstance(resolved.variant, VoxelProbeVariant)

        expected_hash = hashlib.sha256(inspect.getsource(VoxelProbeVariant).encode("utf-8")).hexdigest()
        self.assertEqual(resolved.implementation_hash, expected_hash)
        self.assertTrue(resolved.implementation_hash)

        again = self.registry.resolve("voxel_probe", "voxel_probe")
        self.assertEqual(again.implementation_hash, resolved.implementation_hash)

    def test_descriptor_is_stable_and_serializable(self) -> None:
        descriptor = self.plugin.describe()
        self.assertEqual(descriptor.id, "voxel_probe")
        self.assertEqual(descriptor.version, "0.1.0")
        self.assertEqual(descriptor.label, "Voxel probe (experimental)")
        self.assertEqual(descriptor.kind, "surrogate")
        self.assertEqual(descriptor.operation, "geometry")
        self.assertEqual(descriptor.dimensions, [2, 3])
        self.assertEqual(descriptor.fields, ["voxel"])
        self.assertIn("height-field", descriptor.notes)
        self.assertEqual(self.plugin.params_model, VoxelProbeParams)
        self.assertEqual([variant.variant_id for variant in self.plugin.variants], ["voxel_probe"])

    def test_identity_round_trip_preserves_geometry_and_materials(self) -> None:
        domain = _plane_domain()
        level_sets_before = domain.getNumberOfLevelSets()
        materials_before = _material_ids(domain)
        surface_before = _sorted_surface_points(domain)
        self.assertGreater(surface_before.size, 0)

        result = self._variant().execute(_state(domain), VoxelProbeParams(operation="identity"), _context())

        self.assertIsNot(result.domain, domain)
        self.assertEqual(result.domain.getNumberOfLevelSets(), level_sets_before)
        self.assertEqual(_material_ids(result.domain), materials_before)

        surface_after = _sorted_surface_points(result.domain)
        self.assertEqual(surface_after.shape, surface_before.shape)
        deviation = float(np.max(np.abs(surface_after - surface_before)))
        self.assertLessEqual(deviation, 1e-9)

        self.assertEqual(result.metadata["operation"], "identity")
        self.assertEqual(result.metadata["layers"], 1)
        self.assertEqual(result.metadata["removedCells"], 0)
        self.assertEqual(result.metadata["adapter"], "height-field-v1")
        self.assertEqual(result.metadata["voxelShape"], list(result.metadata["voxelShape"]))
        self.assertTrue(result.metadata["materialOrder"])
        self.assertTrue(result.warnings)

        # The caller's domain must not be mutated by a round trip.
        np.testing.assert_array_equal(_sorted_surface_points(domain), surface_before)

    def test_erode_top_layer_lowers_surface_by_about_one_grid_delta(self) -> None:
        domain = _plane_domain()
        top_before = _surface_top(domain)

        result = self._variant().execute(
            _state(domain),
            VoxelProbeParams(operation="erode_top_layer", layers=1),
            _context(),
        )

        top_after = _surface_top(result.domain)
        drop = top_before - top_after
        self.assertGreater(drop, 0.0)
        self.assertLessEqual(abs(drop - GRID_DELTA), 1.5 * GRID_DELTA)
        self.assertGreater(result.metadata["removedCells"], 0)
        self.assertEqual(result.metadata["operation"], "erode_top_layer")
        self.assertEqual(result.metadata["layers"], 1)
        self.assertEqual(result.metadata["adapter"], "height-field-v1")

        # The input domain is still the original plane.
        self.assertAlmostEqual(_surface_top(domain), top_before, places=12)

    def test_erode_top_layer_is_deterministic(self) -> None:
        domain = _plane_domain()
        params = VoxelProbeParams(operation="erode_top_layer", layers=1)

        first = self._variant().execute(_state(domain), params, _context())
        second = self._variant().execute(_state(domain), params, _context())

        first_points = _sorted_surface_points(first.domain)
        second_points = _sorted_surface_points(second.domain)
        np.testing.assert_array_equal(first_points, second_points)
        self.assertEqual(first.metadata, second.metadata)

        first_digest = hashlib.sha256(np.ascontiguousarray(first_points).tobytes()).hexdigest()
        second_digest = hashlib.sha256(np.ascontiguousarray(second_points).tobytes()).hexdigest()
        self.assertEqual(first_digest, second_digest)

    def test_unknown_operation_is_rejected_by_pydantic(self) -> None:
        with self.assertRaises(ValidationError):
            VoxelProbeParams(operation="melt")

    def test_layers_outside_schema_bounds_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            VoxelProbeParams(operation="identity", layers=0)
        with self.assertRaises(ValidationError):
            VoxelProbeParams(operation="identity", layers=9)

    def test_params_forbid_extra_keys(self) -> None:
        with self.assertRaises(ValidationError):
            VoxelProbeParams(operation="identity", thickness=1.0)

    def test_variant_validate_rejects_bad_dimension_and_layer_overflow(self) -> None:
        variant = self._variant()

        bad_dimension = ProcessContext.model_construct(dimension=4, grid_delta=GRID_DELTA)
        issues = variant.validate(bad_dimension, VoxelProbeParams(operation="identity"))
        self.assertTrue(any(issue.path == "dimension" and issue.severity == "error" for issue in issues))

        overflow = VoxelProbeParams.model_construct(operation="erode_top_layer", layers=9)
        issues = variant.validate(_context(), overflow)
        self.assertTrue(any(issue.path == "layers" and issue.severity == "error" for issue in issues))

        self.assertEqual(
            variant.validate(_context(3), VoxelProbeParams(operation="erode_top_layer", layers=8)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
