"""FaradayCageEtching, GeometricTrenchDeposition and SelectiveEpitaxy.

Both experimental models are constructed with the real ViennaPS 4.6.2 bindings
through ``worker.specialized.build_specialized_model`` and executed with a real
native ``ps.Process``. The original audit concluded that the 2D FaradayCage
path never returns; re-probing with the engine actually pinned showed that the
hang belongs to the AUTO/GPU kernel, not to 2D. On CPU_DISK every cageAngle /
tiltAngle combination in the catalog range completes (see
``worker/probe_faraday_2d.py``), so 2D is exposed behind a strict CPU-only
engine gate and covered by a direct native reference. The tests also pin the
native semantics that the catalog cannot change:

* ``FaradayCageEtching`` wraps ``IBEParameters`` in ``FaradayCageParameters``
  and builds a two-direction periodic cage source. Mask materials are forced
  to a zero plane-wafer rate by the native constructor. Both IBE models require
  ``thresholdEnergy < meanEnergy``: at or above the mean the native yield sign
  reverses and an etch grows the surface (or aborts/empties at equality).
  A small negative ``tiltAngle`` still etches; it is not a no-op.
* ``GeometricTrenchDeposition`` is a ViennaLS geometric-advection distribution.
  It grows the selectable ``depositMaterial`` top level set, ignores
  ``trenchWidth`` and the process duration, and evaluates its local radius from
  absolute z with the trench top assumed at z=0. Its 3D SIGSEGV audit case is
  reproduced as a parameter-reach failure that also occurs in 2D; a rate below
  ``max(a+b, bottomMed)`` can leave an empty deposited level set, so validation
  guards the reach with that conservative bound rather than hiding the dimension.
* ``SelectiveEpitaxy`` binds a separate ``nvFactors`` constructor overload with
  a fixed-size three-vector. The native defaults are dimension-dependent, so
  omitting the key keeps the existing materialRates overload byte-for-byte.
* ``CoverageParameters.tolerance`` defaults natively to 0.0 but the worker has
  always pinned 1e-3. An explicit ``coverageTolerance`` is passed to the same
  native object and read back through ``toMetaData``.

The two experimental models are marked ``experimental`` in the catalog: no Stage
8/9 direct-reference acceptance row has been archived, so validation emits a
warning instead of claiming verified coverage.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import shutil
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
GDS_FIXTURE = ROOT / "tests" / "fixtures" / "finfet22_workbench.gds"
GDS_MASK_FIXTURE = ROOT / "docs" / "gds-mask-diagnosis" / "fixtures" / "multi-layer.gds"
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
sys.path.insert(0, str(ROOT / "worker"))

import viennaps as ps  # noqa: E402
import mesh3d  # noqa: E402

from backend import storage  # noqa: E402
from backend.main import app  # noqa: E402
from backend.hashing import requested_input_hash  # noqa: E402
from backend.models import ProjectV7  # noqa: E402
from backend.specialized_models import CATALOG, model_spec  # noqa: E402
from backend.validation import ProjectInputError, select_engine, validate_project  # noqa: E402
from backend.engine import engine_version  # noqa: E402
from stage8_project_factory import make_project  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from worker.specialized import build_specialized_model  # noqa: E402
from worker.simulate import (MATERIAL_ROWS, configure_advection, configure_rays,
                             make_v3_geometry)  # noqa: E402

FARADAY_FIELDS = {
    "cageAngle", "meanEnergy", "sigmaEnergy", "thresholdEnergy", "thetaRMin", "thetaRMax",
    "minAngle", "inflectAngle", "exponent", "n_l", "tiltAngle", "planeWaferRate",
    "redepositionRate", "redepositionThreshold", "cos4YieldA1", "cos4YieldA2",
    "cos4YieldA3", "cos4YieldA4", "materialPlaneWaferRateByMaterial",
}
TRENCH_FIELDS = {"trenchWidth", "trenchDepth", "depositionRate", "bottomMed", "a", "b", "n"}


def material_handle(name: str):
    return getattr(ps.Material, name)


# ViennaPS 4.7.0 returns etchedDepth 0.0 for the same 2D FaradayCageEtching
# call (worker/probe_faraday_2d.py: 4.6.2 etches 0.000671 um, 4.7.0 etches
# nothing), so the shared gate rejects it instead of running a silent no-op.
FARADAY_2D_ETCHES = engine_version() == "4.6.2"


def measured_engine(version: str = "4.6.2"):
    """Pin the runtime a pre-gate contract was measured against.

    ViennaPS 4.7.0 returns etchedDepth 0.0 for 2D FaradayCageEtching and drops
    every ion after the first on GPU engines. Contracts that describe the
    4.6.2-era behaviour pin the runtime instead of asserting it on 4.7.0.
    """
    if engine_version() == version:
        return nullcontext()
    return patch("backend.engine.engine_version", return_value=version)


def set_boundary(project: dict, boundary: str) -> None:
    if project["dimension"] == 2:
        project["domain"]["xBoundary"] = boundary
    else:
        project["domain"]["boundaryXY"] = boundary


def faraday_project(dimension: int, *, periodic: bool = True) -> dict:
    """Mask below the top film so the cage etches the exposed SiO2."""
    project = make_project("faradayCage", dimension)
    project["geometry"]["layers"] = [
        {"id": "nitride", "name": "Nitride", "material": "Si3N4", "thickness": 0.4, "role": "mask"},
        {"id": "oxide", "name": "Oxide", "material": "SiO2", "thickness": 0.4, "role": "film"},
    ]
    project["steps"][0]["duration"] = 1.0
    set_boundary(project, "periodic" if periodic else "reflective")
    return project


def geometric_project(dimension: int) -> dict:
    project = make_project("geometricTrenchDeposition", dimension)
    project["geometry"] = ({"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}
                           if dimension == 2 else
                           {"kind": "trench", "width": 1.0, "depth": 2.0, "taperAngle": 0.0,
                            "maskHeight": 0.4, "maskTaperAngle": 0.0, "substrateMaterial": "Si"})
    project["steps"][0]["depositMaterial"] = "Al2O3"
    return project


def geometric_plane_project(dimension: int) -> dict:
    project = make_project("geometricTrenchDeposition", dimension)
    project["geometry"] = {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}
    project["steps"][0]["depositMaterial"] = "Al2O3"
    return project


def gds_stack_project(depth: float) -> dict:
    """Stack + legacy GDS overlay mask whose top is at z=1.6 um."""
    project = make_project("geometricTrenchDeposition", 3)
    project["domain"].update({"gridDelta": 0.2, "xExtent": 2.0, "yExtent": 2.0})
    project["geometry"] = {
        "kind": "stack", "substrateMaterial": "Si", "baseHeight": 0.0,
        "layers": [
            {"id": "oxide", "name": "Oxide", "material": "SiO2", "thickness": 0.4, "role": "film"},
            {"id": "nitride", "name": "Nitride", "material": "Si3N4", "thickness": 0.4, "role": "film"},
        ],
        "pattern": {"kind": "none"},
        "gdsMask": {
            "name": "GDS Mask",
            "assetId": hashlib.sha256(GDS_MASK_FIXTURE.read_bytes()).hexdigest(),
            "fileName": GDS_MASK_FIXTURE.name, "structure": "MULTI",
            "layer": 2, "datatype": 0, "unitScale": 1.0,
            "position": [0.0, 0.0, 0.0], "baseHeight": 0.8, "height": 0.8,
            "material": "Mask", "retainedRegion": "inside", "protectsEtch": True,
        },
    }
    project["steps"][0]["depositMaterial"] = "Al2O3"
    project["steps"][0]["nativeParameters"].update({
        "trenchWidth": 1.0, "trenchDepth": depth, "depositionRate": 0.5,
        "bottomMed": 0.2, "a": 0.3, "b": 0.1, "n": 1.0})
    return project


def selective_project(dimension: int, *, nv_factors=None) -> dict:
    """Flat Si surface so the normal is exactly vertical and deterministic."""
    project = make_project("selectiveEpitaxy", dimension)
    project["geometry"] = {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}
    step = project["steps"][0]
    step["duration"] = 1.0
    step["nativeParameters"]["materialRates"] = {"Si": 0.5, "SiGe": 0.5}
    # The upstream selectiveEpitaxy example uses the stencil local Lax-Friedrichs
    # scheme; the default Engquist-Osher scheme does not grow this flat surface.
    project["solver"]["advection"]["spatialScheme"] = "STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER"
    if nv_factors is not None:
        step["nativeParameters"]["nvFactors"] = (list(nv_factors)
                                                  if isinstance(nv_factors, (list, tuple))
                                                  else nv_factors)
    return project


def plasma_project(dimension: int = 2) -> dict:
    project = make_project("sf6o2", dimension)
    project["steps"][0]["duration"] = 0.02
    return project


def native_stack(dimension: int, boundary: str = "periodic"):
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    delta, extent = 0.2, 2.0
    boundary_type = (ps.BoundaryType.PERIODIC_BOUNDARY if boundary == "periodic"
                     else ps.BoundaryType.REFLECTIVE_BOUNDARY)
    domain = (ps.Domain(delta, extent, boundary=boundary_type) if dimension == 2
              else ps.Domain(delta, extent, extent, boundary_type))
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    ps.MakePlane(domain, height=0.4, material=ps.Material.Si3N4, addToExisting=True).apply()
    ps.MakePlane(domain, height=0.8, material=ps.Material.SiO2, addToExisting=True).apply()
    return domain


def native_plane(dimension: int, *, grid: float = 0.2, extent: float = 2.0):
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    delta = grid
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY
    domain = (ps.Domain(delta, extent, boundary=boundary) if dimension == 2
              else ps.Domain(delta, extent, extent, boundary))
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def native_trench(dimension: int):
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    delta, extent = 0.2, 4.0
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY
    domain = (ps.Domain(delta, extent, boundary=boundary) if dimension == 2
              else ps.Domain(delta, extent, extent, boundary))
    ps.MakeTrench(domain=domain, trenchWidth=1.0, trenchDepth=2.0, trenchTaperAngle=0.0,
                  maskHeight=0.4, maskTaperAngle=0.0, halfTrench=False,
                  material=ps.Material.Si, maskMaterial=ps.Material.Si3N4).apply()
    return domain


def surface_signature(domain, dimension: int) -> tuple:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    points = tuple(tuple(round(float(value), 12) for value in point[:dimension])
                   for point in mesh.getNodes())
    cells = mesh.getLines() if dimension == 2 else mesh.getTriangles()
    connectivity = tuple(tuple(int(index) for index in cell) for cell in cells)
    return points, connectivity


def surface_top(domain, dimension: int) -> float:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    vertical = 1 if dimension == 2 else 2
    return max(float(point[vertical]) for point in mesh.getNodes())


def material_ids(domain) -> list[int]:
    return [int(domain.getMaterialMap().getMaterialIdAtIdx(index))
            for index in range(domain.getMaterialMap().size())]


def run_specialized(project: dict, domain) -> tuple[object, dict]:
    parsed, _warnings = validate_project(project, kind="simulate")
    normalized = parsed.model_dump(mode="json", exclude_none=True)
    step = normalized["steps"][0]
    if step["nativeModel"] == "selectiveEpitaxy":
        domain.duplicateTopLevelSet(ps.Material.SiGe)
    model, extra, actual, duration = build_specialized_model(
        ps, domain, normalized, step, material_handle, {}, flux_engine="CPU_DISK")
    process = ps.Process(domain, model, duration)
    if step["nativeModel"] in {"faradayCage", "sf6o2"}:
        # Same schema-7 ray parameters the bounded worker applies.
        process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
        rays, _ray_actual = configure_rays(ps, normalized, 271828)
        process.setParameters(rays)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = 0.25
    if step["nativeModel"] == "selectiveEpitaxy":
        advection.spatialScheme = ps.ls.SpatialSchemeEnum.STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER
    process.setParameters(advection)
    for item in extra:
        process.setParameters(item)
    process.apply()
    return model, actual


class SpecializedCatalogEntries(unittest.TestCase):
    def test_catalog_exposes_the_two_native_constructors(self):
        specs = {row["id"]: row for row in CATALOG}
        faraday = specs["faradayCage"]
        self.assertEqual(faraday["nativeClass"], "FaradayCageEtching")
        self.assertEqual(faraday["operation"], "etch")
        self.assertTrue(faraday["usesFlux"])
        self.assertTrue(faraday["experimental"])
        self.assertEqual(faraday["dimensions"], [2, 3])
        self.assertEqual(faraday["fluxEnginesByDimension"], {"2": ["CPU_DISK"], "3": None})
        self.assertEqual({field["key"] for field in faraday["fields"]}, FARADAY_FIELDS)
        cage_angle = next(field for field in faraday["fields"] if field["key"] == "cageAngle")
        self.assertEqual(cage_angle["default"], 0)

        trench = specs["geometricTrenchDeposition"]
        self.assertEqual(trench["nativeClass"], "GeometricTrenchDeposition")
        self.assertEqual(trench["operation"], "deposit")
        self.assertFalse(trench["usesFlux"])
        self.assertTrue(trench["experimental"])
        self.assertEqual(trench["dimensions"], [2, 3])
        self.assertTrue(trench["depositMaterialSelectable"])
        self.assertEqual({field["key"] for field in trench["fields"]}, TRENCH_FIELDS)
        defaults = {field["key"]: field["default"] for field in trench["fields"]}
        self.assertEqual(defaults, {"trenchWidth": 1.0, "trenchDepth": 4.0, "depositionRate": 0.5,
                                    "bottomMed": 0.2, "a": 0.3, "b": 0.1, "n": 1.0})
        self.assertNotIn("depositMaterial", trench)

    def test_optional_native_fields_are_strict_and_documented(self):
        specs = {row["id"]: row for row in CATALOG}
        selective = specs["selectiveEpitaxy"]
        self.assertEqual(selective["optionalKeys"], ["nvFactors"])
        nv_field = next(field for field in selective["optionalFields"] if field["key"] == "nvFactors")
        self.assertEqual(nv_field["kind"], "nvFactors")
        self.assertEqual(nv_field["length"], 3)
        self.assertEqual((nv_field["minimum"], nv_field["maximum"]), (0.0, 1.0))
        self.assertEqual(nv_field["nativeDefaultByDimension"], {"2": [0.5, 1.0, 0.0], "3": [0.5, 0.0, 1.0]})
        for model_id in ("sf6o2", "cf4o2", "hbro2", "sf6c4f8"):
            with self.subTest(model=model_id):
                self.assertIn("coverageTolerance", specs[model_id]["optionalKeys"])
                field = next(row for row in specs[model_id]["optionalFields"]
                             if row["key"] == "coverageTolerance")
                self.assertEqual(field["nativeDefault"], 0.0)
                self.assertEqual(field["workbenchHistoricalDefault"], 1e-3)
                self.assertEqual((field["minimum"], field["maximum"]), (0.0, 1.0))

    def test_both_models_validate_in_their_supported_dimensions(self):
        for factory, dimension, engine in ((faraday_project, 2, "CPU_DISK"),
                                           (faraday_project, 3, "CPU_DISK"),
                                           (geometric_project, 2, "not-applicable"),
                                           (geometric_project, 3, "not-applicable")):
            with self.subTest(model=factory.__name__, dimension=dimension), measured_engine():
                parsed, warnings = validate_project(factory(dimension), kind="simulate")
                self.assertEqual(parsed.schemaVersion, 7)
                self.assertTrue(any("no archived Stage 8/9 direct-reference acceptance" in warning
                                    for warning in warnings))
                normalized = parsed.model_dump(mode="json", exclude_none=True)
                self.assertEqual(select_engine(normalized, normalized["steps"][0]), engine)
        # Admission is dimension-gated in the catalog, not by a hidden 2D block.
        self.assertIn(2, model_spec("faradayCage")["dimensions"])
        self.assertIn(3, model_spec("faradayCage")["dimensions"])


class Faraday2DProbeMatrix(unittest.TestCase):
    """The committed probe must stay bounded and must not bypass the gate."""

    def test_safe_matrix_covers_the_reported_hang_and_ok_regions_on_cpu_only(self):
        from worker.probe_faraday_2d import _cases_for

        cases = _cases_for("safe-2d")
        pairs = {(float(case["cageAngle"]), float(case["tiltAngle"]))
                 for case in cases if "ibe" not in case and case.get("geometry", "stack") == "stack"}
        for cage in (0.0, 0.2, 10.0, 30.0, 45.0, 60.0, 89.0, 90.0, 180.0):
            for tilt in (0.0, 30.0, 60.0):
                self.assertIn((cage, tilt), pairs)
        self.assertTrue(all(case["engine"] == "CPU_DISK" for case in cases))
        self.assertFalse(any(case.get("probeBypassSafetyGate") for case in cases))

    def test_gpu_and_engine_control_matrices_are_explicitly_probe_only(self):
        from worker.probe_faraday_2d import _cases_for

        for matrix in ("gpu-2d", "engine-control-2d"):
            with self.subTest(matrix=matrix):
                cases = _cases_for(matrix)
                self.assertTrue(cases)
                self.assertTrue(all(case["probeBypassSafetyGate"] for case in cases))


class SpecializedValidationNegatives(unittest.TestCase):
    def test_unknown_native_field_is_rejected(self):
        for factory, dimension in ((faraday_project, 3), (geometric_project, 2)):
            project = factory(dimension)
            project["steps"][0]["nativeParameters"]["notANativeField"] = 1
            with self.assertRaises(ProjectInputError) as ctx:
                validate_project(project)
            self.assertTrue(any("nativeParameters" in issue.message for issue in ctx.exception.issues))

    def test_non_finite_and_out_of_range_values_are_rejected(self):
        project = faraday_project(3)
        project["steps"][0]["nativeParameters"]["cageAngle"] = float("nan")
        with self.assertRaises(ProjectInputError):
            validate_project(project)
        project = faraday_project(3)
        project["steps"][0]["nativeParameters"]["cageAngle"] = 200
        with self.assertRaises(ProjectInputError):
            validate_project(project)
        project = geometric_project(2)
        project["steps"][0]["nativeParameters"]["trenchDepth"] = 0
        with self.assertRaises(ProjectInputError):
            validate_project(project)

    def test_geometric_trench_rejects_fractional_n_and_zero_geometry(self):
        for key, value in (("n", 0.5), ("trenchWidth", 0), ("depositionRate", 0)):
            with self.subTest(key=key):
                project = geometric_project(2)
                project["steps"][0]["nativeParameters"][key] = value
                with self.assertRaises(ProjectInputError) as ctx:
                    validate_project(project)
                self.assertTrue(any(key in issue.path for issue in ctx.exception.issues),
                                [issue.path for issue in ctx.exception.issues])

    def test_geometric_deposition_rejects_gds_geometry(self):
        from unittest.mock import patch
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                project = geometric_project(dimension)
                project["geometry"] = {
                    "kind": "gds", "assetId": hashlib.sha256(GDS_FIXTURE.read_bytes()).hexdigest(),
                    "fileName": GDS_FIXTURE.name, "structure": "FINFET22", "unitScale": 1.0,
                    "position": [0.0, 0.0, 0.0],
                    "layers": ([{"layer": 7, "material": "Si"}] if dimension == 2 else
                               [{"layer": 7, "material": "Si", "baseHeight": 0.0, "height": 0.4}]),
                }
                with patch("backend.storage.resolve_gds_asset", return_value=GDS_FIXTURE):
                    with self.assertRaises(ProjectInputError) as ctx:
                        validate_project(project)
                self.assertTrue(any("GDS geometry" in issue.message for issue in ctx.exception.issues),
                                [issue.message for issue in ctx.exception.issues])

    def test_geometric_parameter_reach_guard_rejects_the_audit_crash_case(self):
        # This is the exact 3D probe from the capability audit: a 2.1 um native
        # radius with depositionRate=0.05 and gridDelta=0.1. It SIGSEGVs in both
        # 2D and 3D; the worker now requires rate >= max(a+b, bottomMed).
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                project = geometric_project(dimension)
                project["steps"][0]["nativeParameters"].update({
                    "trenchWidth": 0.4, "trenchDepth": 0.5, "depositionRate": 0.05,
                    "bottomMed": 0.1, "a": 0.1, "b": 2.0, "n": 1.0})
                with self.assertRaises(ProjectInputError) as ctx:
                    validate_project(project)
                messages = [issue.message for issue in ctx.exception.issues
                            if issue.path == "steps[0].nativeParameters.depositionRate"]
                self.assertTrue(messages, [issue.path for issue in ctx.exception.issues])
                self.assertIn("native radius", messages[0])

    def test_geometric_reach_guard_rejects_the_empty_layer_boundary(self):
        # Repro from the review: rate 0.15 (and even 0.2, which the kernel
        # happens to accept) is below the native radius 0.4. The conservative
        # guard rejects both rather than risking an empty deposited level set.
        for dimension in (2, 3):
            for rate in (0.15, 0.2):
                with self.subTest(dimension=dimension, rate=rate):
                    project = geometric_plane_project(dimension)
                    project["domain"]["gridDelta"] = 0.1
                    project["domain"]["xExtent"] = 2.0
                    if dimension == 3:
                        project["domain"]["yExtent"] = 2.0
                    project["steps"][0]["nativeParameters"].update({
                        "trenchWidth": 1.0, "trenchDepth": 0.5, "depositionRate": rate,
                        "bottomMed": 0.1, "a": 0.3, "b": 0.1, "n": 1.0})
                    with self.assertRaises(ProjectInputError) as ctx:
                        validate_project(project)
                    messages = [issue.message for issue in ctx.exception.issues
                                if issue.path == "steps[0].nativeParameters.depositionRate"]
                    self.assertTrue(messages, [issue.path for issue in ctx.exception.issues])
                    self.assertIn("empty", messages[0])

    def test_geometric_fin_vertical_extent_includes_the_mask(self):
        # 3D fin height 0.8 + mask 0.8 puts the top surface at |z|=1.6, not 0.8.
        # A native depth of 0.8 passed the old max(height, maskHeight) bound and
        # reduced the fin to z=0.5 while emptying the substrate level set.
        project = geometric_project(3)
        project["geometry"] = {
            "kind": "fin", "width": 1.0, "height": 0.8, "taperAngle": 0.0,
            "maskHeight": 0.8, "maskTaperAngle": 0.0, "substrateMaterial": "Si", "maskMaterial": "Mask"}
        project["steps"][0]["nativeParameters"].update({"trenchDepth": 0.8, "depositionRate": 0.5})
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project)
        messages = [issue.message for issue in ctx.exception.issues
                    if issue.path == "steps[0].nativeParameters.trenchDepth"]
        self.assertTrue(messages, [(issue.path, issue.message) for issue in ctx.exception.issues])
        self.assertIn("1.6", messages[0])

    def test_geometric_stack_gds_mask_participates_in_the_vertical_bound(self):
        # The legacy overlay mask is a separate body above the stack: its top is
        # baseHeight + position[2] + height = 1.6 um, while the stack itself is
        # only 0.8 um. A native depth of 0.8 used to pass and SIGSEGV.
        from unittest.mock import patch
        with patch("backend.storage.resolve_gds_asset", return_value=GDS_MASK_FIXTURE):
            with self.assertRaises(ProjectInputError) as ctx:
                validate_project(gds_stack_project(0.8))
            messages = [issue.message for issue in ctx.exception.issues
                        if issue.path == "steps[0].nativeParameters.trenchDepth"]
            self.assertTrue(messages, [(issue.path, issue.message) for issue in ctx.exception.issues])
            self.assertIn("1.6", messages[0])
            parsed, _warnings = validate_project(gds_stack_project(1.6))
        self.assertEqual(parsed.steps[0].nativeParameters["trenchDepth"], 1.6)

    def test_geometric_rejects_zero_radius_and_sub_grid_width_depth(self):
        cases = (
            ("a", 0.0, "a+b and bottomMed"),
            ("trenchWidth", 0.01, "at least one grid interval"),
            ("trenchDepth", 0.01, "at least one grid interval"),
        )
        for key, value, message in cases:
            with self.subTest(key=key):
                project = geometric_project(3)
                if key == "a":
                    project["steps"][0]["nativeParameters"].update({"a": 0.0, "b": 0.0, "bottomMed": 0.0})
                else:
                    project["steps"][0]["nativeParameters"][key] = value
                with self.assertRaises(ProjectInputError) as ctx:
                    validate_project(project)
                self.assertTrue(any(message in issue.message for issue in ctx.exception.issues),
                                [(issue.path, issue.message) for issue in ctx.exception.issues])

    def test_selective_nv_factors_reject_unknown_length_range_and_all_zero(self):
        for value in ([1, 1], [1, 1, 1, 1], [-0.1, 1, 1], [1.1, 1, 1],
                      [float("nan"), 1, 1], [float("inf"), 1, 1], [0, 0, 0],
                      [10 ** 400, 1, 1], "not-a-vector", True):
            with self.subTest(value=value):
                project = selective_project(2, nv_factors=value)
                with self.assertRaises(ProjectInputError) as ctx:
                    validate_project(project)
                paths = [issue.path for issue in ctx.exception.issues]
                self.assertTrue(any("nvFactors" in path or "nativeParameters" in path for path in paths), paths)
        project = selective_project(2)
        project["steps"][0]["nativeParameters"]["nvFactorsX"] = [1, 1, 1]
        with self.assertRaises(ProjectInputError):
            validate_project(project)

    def test_coverage_tolerance_rejects_invalid_range_and_wrong_models(self):
        for value in (-1.0, 1.5, float("nan"), float("inf"), True, "0.1"):
            with self.subTest(value=value):
                project = plasma_project(2)
                project["steps"][0]["nativeParameters"]["coverageTolerance"] = value
                with self.assertRaises(ProjectInputError) as ctx:
                    validate_project(project)
                self.assertTrue(any("coverageTolerance" in issue.path for issue in ctx.exception.issues),
                                [issue.path for issue in ctx.exception.issues])
        project = selective_project(2)
        project["steps"][0]["nativeParameters"]["coverageTolerance"] = 1e-3
        with self.assertRaises(ProjectInputError):
            validate_project(project)

    def test_geometric_deposition_must_be_the_first_enabled_step(self):
        project = geometric_project(2)
        project["steps"].insert(0, {
            "id": "pre_deposition", "name": "pre", "enabled": True, "operation": "deposit",
            "model": "isotropic", "rateMagnitude": 0.1, "duration": 1.0,
            "depositMaterial": "SiO2",
        })
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project)
        self.assertTrue(any(issue.path == "steps[1].nativeModel" for issue in ctx.exception.issues),
                        [issue.path for issue in ctx.exception.issues])

    def test_geometric_trench_depth_covers_the_initial_vertical_span(self):
        project = geometric_project(2)
        project["geometry"] = {"kind": "plane", "height": 8.0, "substrateMaterial": "Si"}
        project["steps"][0]["nativeParameters"].update({"trenchDepth": 0.000001, "n": 100})
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project)
        self.assertTrue(any("trenchDepth" in issue.path for issue in ctx.exception.issues),
                        [issue.path for issue in ctx.exception.issues])

    def test_geometric_deposition_requires_a_deposit_material(self):
        project = geometric_project(2)
        project["steps"][0].pop("depositMaterial")
        with self.assertRaises(ProjectInputError):
            validate_project(project)
        project = geometric_project(2)
        project["steps"][0]["depositMaterial"] = "not-a-material"
        with self.assertRaises(ProjectInputError):
            validate_project(project)

    def test_catalogue_operation_is_enforced(self):
        project = faraday_project(3)
        project["steps"][0]["operation"] = "deposit"
        project["steps"][0]["depositMaterial"] = "SiO2"
        with self.assertRaises(ProjectInputError):
            validate_project(project)

    def test_material_table_scoping_matches_ion_beam(self):
        project = faraday_project(3)
        del project["geometry"]["layers"][0]  # no Si3N4 mask at this step
        project["steps"][0]["nativeParameters"]["materialPlaneWaferRateByMaterial"] = {"Si3N4": 0.01}
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project, kind="simulate")
        paths = [issue.path for issue in ctx.exception.issues]
        self.assertIn("steps[0].nativeParameters.materialPlaneWaferRateByMaterial", paths)

    def test_2d_faraday_rejects_auto_and_gpu_engines_with_422(self):
        # The GPU kernel hangs over a large cage/tilt region, and specialized
        # AUTO resolves to GPU_DISK. Both are rejected before a worker starts.
        for preference, gpu_engine in (("auto", None), ("gpu", None), ("gpu", "GPU_TRIANGLE")):
            with self.subTest(preference=preference, gpuEngine=gpu_engine):
                project = faraday_project(2)
                project["solver"]["enginePreference"] = preference
                if gpu_engine is not None:
                    project["solver"]["gpuEngine"] = gpu_engine
                with self.assertRaises(ProjectInputError) as ctx:
                    validate_project(project, kind="simulate")
                messages = [issue.message for issue in ctx.exception.issues
                            if issue.path == "solver.enginePreference"]
                self.assertTrue(messages, [issue.path for issue in ctx.exception.issues])
                if engine_version() == "4.6.2":
                    self.assertIn("CPU_DISK", messages[0])
                    self.assertIn("AUTO/GPU_DISK/GPU_TRIANGLE", messages[0])
                else:
                    # 4.7.0 rejects every 2D Faraday cage engine, CPU_DISK
                    # included, because the kernel returns a zero velocity.
                    self.assertIn("produces no etch", messages[0])

    def test_2d_faraday_engine_gate_skips_disabled_drafts_and_keeps_3d(self):
        # A saved draft with the 2D step disabled stays readable; enabling it
        # re-runs the gate.
        draft = faraday_project(2)
        draft["solver"]["enginePreference"] = "auto"
        draft["steps"][0]["enabled"] = False
        parsed, _warnings = validate_project(draft)
        self.assertFalse(parsed.steps[0].enabled)
        draft["steps"][0]["enabled"] = True
        with self.assertRaises(ProjectInputError):
            validate_project(draft)
        # 3D must stay exactly as before: AUTO is still accepted and still
        # resolves to GPU_DISK for this flux model.
        three_d = faraday_project(3)
        three_d["solver"]["enginePreference"] = "auto"
        parsed, _warnings = validate_project(three_d, kind="simulate")
        self.assertEqual(parsed.solver.enginePreference, "auto")
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")

    def test_2d_faraday_negative_tilt_warning_is_accurate(self):
        # -0.2 deg is not a no-op (the native run etches 0.8 -> 0.782019 um),
        # so the warning must not promise an unchanged surface.
        project = faraday_project(2)
        project["steps"][0]["nativeParameters"]["tiltAngle"] = -0.2
        with measured_engine():
            _parsed, warnings = validate_project(project, kind="simulate")
        messages = [warning for warning in warnings if "tiltAngle" in warning]
        self.assertTrue(messages, warnings)
        text = messages[0]
        self.assertIn("can still etch", text)
        self.assertIn("not symmetric", text)
        for false_claim in ("no etch", "no-op", "unchanged surface"):
            self.assertNotIn(false_claim, text)

    def test_ibe_energy_values_reject_strings_bools_and_non_finite(self):
        # ProjectV7.nativeParameters is dict[str, Any], so strings survive
        # model_validate; the shared rule must still reject them instead of
        # letting the worker coerce "200" with float().
        for dimension in (2, 3):
            for key, value in (("thresholdEnergy", "200"), ("meanEnergy", "100"),
                               ("thresholdEnergy", True), ("meanEnergy", False),
                               ("thresholdEnergy", float("nan")),
                               ("meanEnergy", float("inf"))):
                with self.subTest(dimension=dimension, key=key, value=value):
                    project = faraday_project(dimension)
                    project["steps"][0]["nativeParameters"][key] = value
                    with self.assertRaises(ProjectInputError) as ctx:
                        validate_project(project, kind="simulate")
                    self.assertTrue(any(issue.path == f"steps[0].nativeParameters.{key}"
                                        for issue in ctx.exception.issues),
                                    [(issue.path, issue.message)
                                     for issue in ctx.exception.issues])

    def test_ibe_energy_order_rejects_threshold_at_or_above_mean(self):
        # Native normalization reverses the flux sign at threshold >= mean, so
        # the same rule must reject 2D and 3D before the worker.
        for dimension in (2, 3):
            for threshold, mean, sigma in ((200.0, 100.0, 500.0),
                                           (100.0, 100.0, 10.0),
                                           (100.0, 100.0, 0.0),
                                           (101.0, 100.0, 10.0)):
                with self.subTest(dimension=dimension, threshold=threshold,
                                  mean=mean, sigma=sigma), measured_engine():
                    project = faraday_project(dimension)
                    project["steps"][0]["nativeParameters"].update({
                        "meanEnergy": mean, "thresholdEnergy": threshold,
                        "sigmaEnergy": sigma})
                    with self.assertRaises(ProjectInputError) as ctx:
                        validate_project(project, kind="simulate")
                    messages = [
                        issue.message for issue in ctx.exception.issues
                        if issue.path == "steps[0].nativeParameters.thresholdEnergy"]
                    self.assertTrue(messages,
                                    [(issue.path, issue.message) for issue in ctx.exception.issues])
                    self.assertIn("less than meanEnergy", messages[0])
        # IonBeamEtching wraps the same native IBEParameters struct.
        ion_beam = make_project("ionBeam", 3)
        ion_beam["steps"][0]["nativeParameters"].update({
            "meanEnergy": 100.0, "thresholdEnergy": 200.0})
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(ion_beam, kind="simulate")
        self.assertTrue(any(issue.path == "steps[0].nativeParameters.thresholdEnergy"
                            for issue in ctx.exception.issues))
        # Just below the mean stays valid (the probe etched at 99.9/100).
        valid = faraday_project(2)
        valid["steps"][0]["nativeParameters"].update({
            "meanEnergy": 100.0, "thresholdEnergy": 99.9, "sigmaEnergy": 500.0})
        with measured_engine():
            parsed, _warnings = validate_project(valid, kind="simulate")
        self.assertEqual(parsed.steps[0].nativeParameters["thresholdEnergy"], 99.9)


class WorkerSafetyPreflight(unittest.TestCase):
    """Direct worker calls must apply the same safety rules as the API."""

    def normalized(self, project: dict) -> tuple[dict, dict]:
        normalized = ProjectV7.model_validate(project).model_dump(mode="json", exclude_none=True)
        return normalized, normalized["steps"][0]

    def test_direct_worker_rejects_unsafe_geometric_trench(self):
        project = geometric_plane_project(3)
        project["steps"][0]["nativeParameters"].update({
            "trenchWidth": 0.4, "trenchDepth": 0.5, "depositionRate": 0.05,
            "bottomMed": 0.1, "a": 0.1, "b": 2.0, "n": 1.0})
        normalized, step = self.normalized(project)
        with self.assertRaises(RuntimeError) as ctx:
            build_specialized_model(ps, native_plane(3), normalized, step, material_handle, {})
        message = str(ctx.exception)
        self.assertIn("GeometricTrenchDeposition rejected", message)
        self.assertIn("native radius", message)

    def test_direct_worker_rejects_stack_gds_mask_depth_0_8(self):
        normalized, step = self.normalized(gds_stack_project(0.8))
        with self.assertRaises(RuntimeError) as ctx:
            build_specialized_model(ps, None, normalized, step, material_handle, {})
        message = str(ctx.exception)
        self.assertIn("GeometricTrenchDeposition rejected", message)
        self.assertIn("1.6", message)

    def test_direct_worker_rejects_negative_coverage_tolerance(self):
        for value in (-1.0, 1.5, float("nan")):
            with self.subTest(value=value):
                project = plasma_project(2)
                project["steps"][0]["nativeParameters"]["coverageTolerance"] = value
                normalized, step = self.normalized(project)
                with self.assertRaises(RuntimeError) as ctx:
                    build_specialized_model(ps, None, normalized, step, material_handle, {})
                self.assertIn("coverageTolerance", str(ctx.exception))

    def test_direct_worker_rejects_oversized_nv_factors(self):
        project = selective_project(2, nv_factors=[10 ** 400, 1, 1])
        normalized, step = self.normalized(project)
        with self.assertRaises(RuntimeError) as ctx:
            build_specialized_model(ps, None, normalized, step, material_handle, {})
        self.assertIn("nvFactors", str(ctx.exception))

    def test_direct_worker_rejects_unpinned_or_gpu_2d_faraday(self):
        # Direct worker input never passes backend.validation, so the shared
        # gate must reject before any FaradayCageParameters object is built.
        normalized, step = self.normalized(faraday_project(2))
        for engine in (None, "AUTO", "GPU_DISK", "GPU_TRIANGLE"):
            with self.subTest(engine=engine), measured_engine():
                with self.assertRaises(RuntimeError) as ctx:
                    build_specialized_model(ps, native_stack(2, "periodic"), normalized, step,
                                            material_handle, {}, flux_engine=engine)
                message = str(ctx.exception)
                self.assertIn("FaradayCageEtching rejected", message)
                self.assertIn("CPU_DISK", message)
        if not FARADAY_2D_ETCHES:
            # On 4.7.0 even a pinned CPU_DISK 2D model is refused before the
            # native call (the kernel returns a zero velocity).
            with self.assertRaises(RuntimeError) as ctx:
                build_specialized_model(ps, native_stack(2, "periodic"), normalized, step,
                                        material_handle, {}, flux_engine="CPU_DISK")
            self.assertIn("produces no etch", str(ctx.exception))

    @unittest.skipUnless(FARADAY_2D_ETCHES,
                         "ViennaPS 4.7.0 gates the 2D FaradayCageEtching no-op")
    def test_direct_worker_admits_2d_faraday_on_cpu_disk(self):
        normalized, step = self.normalized(faraday_project(2))
        model, _extra, actual, _duration = build_specialized_model(
            ps, native_stack(2, "periodic"), normalized, step, material_handle, {},
            flux_engine="CPU_DISK")
        self.assertEqual(type(model).__name__, "FaradayCageEtching")
        self.assertEqual(type(model).__module__, "viennaps.d2")
        container = actual["nativeParameterContainer"]
        self.assertEqual(container["dimension"], 2)
        self.assertEqual(container["fluxEngine"], "CPU_DISK")
        self.assertEqual(container["fluxEngineGate"], "CPU_DISK only in 2D")

    def test_direct_worker_rejects_threshold_at_or_above_mean(self):
        # Direct worker input reaches build_specialized_model without
        # backend.validation; the energy rule must fire before the native
        # IBEParameters object exists (domain=None proves that).
        for dimension in (2, 3):
            for threshold, mean, sigma in ((200.0, 100.0, 500.0),
                                           (100.0, 100.0, 10.0),
                                           (100.0, 100.0, 0.0)):
                with self.subTest(dimension=dimension, threshold=threshold,
                                  mean=mean, sigma=sigma), measured_engine():
                    project = faraday_project(dimension)
                    project["steps"][0]["nativeParameters"].update({
                        "meanEnergy": mean, "thresholdEnergy": threshold,
                        "sigmaEnergy": sigma})
                    normalized, step = self.normalized(project)
                    with self.assertRaises(RuntimeError) as ctx:
                        build_specialized_model(ps, None, normalized, step,
                                                material_handle, {},
                                                flux_engine="CPU_DISK")
                    message = str(ctx.exception)
                    self.assertIn("FaradayCageEtching rejected", message)
                    self.assertIn("thresholdEnergy", message)
                    self.assertIn("less than meanEnergy", message)
        # IonBeamEtching shares the native struct and the same rule.
        ion_beam = make_project("ionBeam", 3)
        ion_beam["steps"][0]["nativeParameters"].update({
            "meanEnergy": 100.0, "thresholdEnergy": 200.0})
        normalized, step = self.normalized(ion_beam)
        with self.assertRaises(RuntimeError) as ctx:
            build_specialized_model(ps, None, normalized, step, material_handle, {})
        self.assertIn("thresholdEnergy", str(ctx.exception))

    def test_direct_worker_rejects_string_energy_before_native(self):
        # ProjectV7 accepts these strings; the worker must reject them before
        # float() would coerce "200" and reach the native constructor.
        for dimension in (2, 3):
            for key, value in (("thresholdEnergy", "200"), ("meanEnergy", "100")):
                with self.subTest(dimension=dimension, key=key), measured_engine():
                    project = faraday_project(dimension)
                    project["steps"][0]["nativeParameters"].update({
                        "meanEnergy": 100.0, "thresholdEnergy": 20.0,
                        "sigmaEnergy": 500.0})
                    project["steps"][0]["nativeParameters"][key] = value
                    normalized, step = self.normalized(project)
                    self.assertEqual(step["nativeParameters"][key], value)
                    with self.assertRaises(RuntimeError) as ctx:
                        build_specialized_model(ps, None, normalized, step,
                                                material_handle, {},
                                                flux_engine="CPU_DISK")
                    message = str(ctx.exception)
                    self.assertIn("FaradayCageEtching rejected", message)
                    self.assertIn(key, message)
                    self.assertIn("numeric strings are not coerced", message)
        # IonBeamEtching shares the helper and the native struct.
        ion_beam = make_project("ionBeam", 3)
        ion_beam["steps"][0]["nativeParameters"]["thresholdEnergy"] = "200"
        normalized, step = self.normalized(ion_beam)
        with self.assertRaises(RuntimeError) as ctx:
            build_specialized_model(ps, None, normalized, step, material_handle, {})
        self.assertIn("numeric strings are not coerced", str(ctx.exception))
        self.assertIn("thresholdEnergy", str(ctx.exception))


class NativeFaradayCage(unittest.TestCase):
    def test_real_3d_cpu_run_etches_the_exposed_film(self):
        for dimension in (3,):
            with self.subTest(dimension=dimension):
                project = faraday_project(dimension)
                domain = native_stack(dimension, "periodic")
                before = surface_top(domain, dimension)
                model, actual = run_specialized(project, domain)
                after = surface_top(domain, dimension)
                self.assertEqual(type(model).__name__, "FaradayCageEtching")
                self.assertNotIsInstance(model, ps.IonBeamEtching)
                self.assertLess(after, before - 1e-3)
                container = actual["nativeParameterContainer"]
                self.assertEqual(container["nativeClass"], "FaradayCageParameters")
                self.assertEqual(container["dimension"], 3)
                self.assertEqual(container["fluxEngine"], "CPU_DISK")
                self.assertEqual(container["cageAngle"], 0.0)
                self.assertEqual(container["maskMaterials"], ["Si3N4"])
                self.assertEqual(container["ibeParameters"]["meanEnergy"], 100.0)
                self.assertTrue(container["maskMaterialPlaneWaferRateForcedZero"])


class NativeFaradayCage2D(unittest.TestCase):
    """2D is admitted only on CPU_DISK and must match a direct native reference.

    The anchor pairs span the region the original audit reported as hanging
    (cageAngle 0.2 at tiltAngle 0) and the region it reported as working
    (cageAngle 90 at tiltAngle 0/60), so the test pins the corrected boundary
    instead of a single lucky case.
    """

    ANCHORS = ((0.2, 0.0), (45.0, 30.0), (90.0, 0.0), (90.0, 60.0))

    # ViennaPS 4.7.0 returns etchedDepth 0.0 for the same native call (measured
    # with worker/probe_faraday_2d.py: 4.6.2 etches 0.000671 um, 4.7.0 etches
    # nothing), so the shared gate rejects it instead of running a silent no-op.
    ETCHES_2D = FARADAY_2D_ETCHES

    def test_2d_policy_matches_the_installed_runtime(self):
        project = faraday_project(2)
        if self.ETCHES_2D:
            self.assertEqual(validate_project(project, kind="simulate")[0].dimension, 2)
            return
        with self.assertRaises(ProjectInputError) as context:
            validate_project(project, kind="simulate")
        message = " ".join(issue.message for issue in context.exception.issues)
        self.assertIn("2D FaradayCageEtching produces no etch", message)
        self.assertIn(engine_version(), message)
        # 3D keeps working on both runtimes.
        self.assertEqual(validate_project(faraday_project(3), kind="simulate")[0].dimension, 3)

    def direct_reference(self, project: dict, domain):
        normalized = validate_project(project, kind="simulate")[0].model_dump(
            mode="json", exclude_none=True)
        params = normalized["steps"][0]["nativeParameters"]
        ps.setDimension(2)
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.Time.setUnit("s")
        parameters = ps.FaradayCageParameters()
        for key in ("meanEnergy", "sigmaEnergy", "thresholdEnergy", "thetaRMin", "thetaRMax",
                    "minAngle", "inflectAngle", "exponent", "n_l", "tiltAngle",
                    "planeWaferRate", "redepositionRate", "redepositionThreshold"):
            setattr(parameters.ibeParams, key, float(params[key]))
        for index, key in enumerate(("cos4YieldA1", "cos4YieldA2", "cos4YieldA3", "cos4YieldA4"),
                                     start=1):
            setattr(parameters.ibeParams.cos4Yield, f"a{index}", float(params[key]))
        for name, value in sorted(params["materialPlaneWaferRateByMaterial"].items()):
            parameters.ibeParams.materialPlaneWaferRate.set(material_handle(name), float(value))
        parameters.cageAngle = float(params["cageAngle"])
        model = ps.FaradayCageEtching(parameters, [material_handle("Si3N4")])
        process = ps.Process(domain, model, float(normalized["steps"][0]["duration"]))
        process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
        rays, _ray_actual = configure_rays(ps, normalized, 271828)
        process.setParameters(rays)
        advection, _advection_actual = configure_advection(ps, normalized)
        process.setParameters(advection)
        process.apply()
        return domain

    @unittest.skipUnless(FARADAY_2D_ETCHES, "ViennaPS 4.7.0 gates the 2D FaradayCageEtching no-op")
    def test_real_2d_runs_match_a_direct_native_reference(self):
        for cage, tilt in self.ANCHORS:
            with self.subTest(cage=cage, tilt=tilt):
                project = faraday_project(2)
                project["steps"][0]["nativeParameters"]["cageAngle"] = cage
                project["steps"][0]["nativeParameters"]["tiltAngle"] = tilt
                product = native_stack(2, "periodic")
                before = surface_top(product, 2)
                model, actual = run_specialized(project, product)
                after = surface_top(product, 2)
                self.assertEqual(type(model).__name__, "FaradayCageEtching")
                self.assertEqual(type(model).__module__, "viennaps.d2")
                self.assertLess(after, before - 1e-6)
                counts = [int(level_set.getNumberOfPoints())
                          for level_set in product.getLevelSets()]
                self.assertTrue(counts and all(count > 0 for count in counts), counts)
                container = actual["nativeParameterContainer"]
                self.assertEqual(container["dimension"], 2)
                self.assertEqual(container["fluxEngine"], "CPU_DISK")
                self.assertEqual(container["cageAngle"], cage)
                self.assertEqual(container["ibeParameters"]["tiltAngle"], tilt)

                reference = self.direct_reference(project, native_stack(2, "periodic"))
                self.assertEqual(surface_signature(product, 2),
                                 surface_signature(reference, 2))

    @unittest.skipUnless(FARADAY_2D_ETCHES, "ViennaPS 4.7.0 gates the 2D FaradayCageEtching no-op")
    def test_2d_small_negative_tilt_still_etches(self):
        # Regression for the false "unchanged surface" claim: -0.2 deg etches.
        project = faraday_project(2)
        project["steps"][0]["nativeParameters"]["tiltAngle"] = -0.2
        domain = native_stack(2, "periodic")
        before = surface_top(domain, 2)
        model, _actual = run_specialized(project, domain)
        self.assertEqual(type(model).__name__, "FaradayCageEtching")
        self.assertLess(surface_top(domain, 2), before - 1e-6)

    @unittest.skipUnless(FARADAY_2D_ETCHES, "ViennaPS 4.7.0 gates the 2D FaradayCageEtching no-op")
    def test_valid_energy_order_still_etches(self):
        # thresholdEnergy < meanEnergy remains an ordinary etch, including at
        # the near-boundary values that the probe measured.
        for threshold in (20.0, 90.0, 99.9):
            with self.subTest(threshold=threshold):
                project = faraday_project(2)
                project["steps"][0]["nativeParameters"].update({
                    "meanEnergy": 100.0, "thresholdEnergy": threshold,
                    "sigmaEnergy": 500.0})
                domain = native_stack(2, "periodic")
                before = surface_top(domain, 2)
                _model, actual = run_specialized(project, domain)
                self.assertLess(surface_top(domain, 2), before - 1e-6)
                self.assertEqual(
                    actual["nativeParameterContainer"]["ibeParameters"]["thresholdEnergy"],
                    threshold)


class NativeGeometricTrenchDeposition(unittest.TestCase):
    ARGUMENT_KEYS = ("trenchWidth", "trenchDepth", "depositionRate", "bottomMed", "a", "b", "n")

    def run_direct_native(self, domain, dimension: int, parameters: dict):
        model = ps.GeometricTrenchDeposition(
            *(float(parameters[key]) for key in self.ARGUMENT_KEYS))
        process = ps.Process(domain, model, 0.0)
        process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
        advection = ps.AdvectionParameters()
        advection.timeStepRatio = 0.25
        process.setParameters(advection)
        process.apply()
        return domain

    def test_real_2d_cpu_run_adds_the_selectable_material(self):
        for dimension in (2,):
            with self.subTest(dimension=dimension):
                project = geometric_project(dimension)
                domain = native_plane(dimension)
                deposit_material = material_handle(project["steps"][0]["depositMaterial"])
                domain.duplicateTopLevelSet(deposit_material)
                before = surface_top(domain, dimension)
                model, actual = run_specialized(project, domain)
                after = surface_top(domain, dimension)
                self.assertEqual(type(model).__name__, "GeometricTrenchDeposition")
                self.assertEqual(type(model).__module__, "viennaps.d2")
                # radius at z=0 is a + b = 0.4 um.
                self.assertAlmostEqual(after - before, 0.4, delta=0.02)
                self.assertEqual(material_ids(domain)[-1], int(deposit_material.legacyId()))
                semantics = actual["nativeGeometricAdvection"]
                self.assertFalse(semantics["trenchWidthUsedByNativeKernel"])
                self.assertFalse(semantics["durationScalesThickness"])
                self.assertEqual(semantics["depositMaterial"], "Al2O3")
                self.assertIsNone(semantics["nativeMask"])

    def test_real_3d_plane_cpu_run_uses_the_3d_native_class(self):
        project = geometric_plane_project(3)
        domain = native_plane(3)
        domain.duplicateTopLevelSet(ps.Material.Al2O3)
        before = surface_top(domain, 3)
        model, actual = run_specialized(project, domain)
        after = surface_top(domain, 3)
        self.assertEqual(type(model).__name__, "GeometricTrenchDeposition")
        self.assertEqual(type(model).__module__, "viennaps.d3")
        self.assertAlmostEqual(after - before, 0.4, delta=0.02)
        self.assertEqual(actual["nativeGeometricAdvection"]["depositMaterial"], "Al2O3")

    def test_stack_gds_mask_depth_1_6_runs_non_empty(self):
        from unittest.mock import patch
        project = gds_stack_project(1.6)
        with patch("backend.storage.resolve_gds_asset", return_value=GDS_MASK_FIXTURE):
            parsed, _warnings = validate_project(project)
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        run_dir = ROOT / "var" / "test-gds-gtd"
        shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True)
        asset = run_dir / "mask.gds"
        shutil.copyfile(GDS_MASK_FIXTURE, asset)
        try:
            ps.setDimension(3)
            ps.setNumThreads(1)
            ps.Length.setUnit("um")
            ps.Time.setUnit("s")
            mesh3d.configure_material_catalog(MATERIAL_ROWS)
            domain = mesh3d.make_domain(ps, normalized)
            make_v3_geometry(ps, domain, normalized,
                             config={"gdsAssetPath": str(asset)}, run_dir=run_dir)
            domain.duplicateTopLevelSet(ps.Material.Al2O3)
            before = surface_top(domain, 3)
            model, extra, _actual, duration = build_specialized_model(
                ps, domain, normalized, normalized["steps"][0], material_handle, {})
            process = ps.Process(domain, model, duration)
            process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
            advection = ps.AdvectionParameters()
            advection.timeStepRatio = 0.25
            process.setParameters(advection)
            for item in extra:
                process.setParameters(item)
            process.apply()
            counts = [int(level_set.getNumberOfPoints()) for level_set in domain.getLevelSets()]
            self.assertTrue(counts and all(count > 0 for count in counts), counts)
            self.assertGreater(surface_top(domain, 3), before + 1e-6)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_reach_guard_boundary_rate_equals_radius_is_non_empty(self):
        # rate == max(a+b, bottomMed) == 0.4 is the conservative guard boundary.
        # Probe 4.6.2/5.8.5: this boundary completes with every level set
        # populated; rate 0.15/0.2 was empty (0.2 happened to work, 0.15 did not).
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                project = geometric_plane_project(dimension)
                project["domain"]["gridDelta"] = 0.1
                project["domain"]["xExtent"] = 2.0
                if dimension == 3:
                    project["domain"]["yExtent"] = 2.0
                project["steps"][0]["nativeParameters"].update({
                    "trenchWidth": 1.0, "trenchDepth": 0.5, "depositionRate": 0.4,
                    "bottomMed": 0.1, "a": 0.3, "b": 0.1, "n": 1.0})
                domain = native_plane(dimension, grid=0.1, extent=2.0)
                domain.duplicateTopLevelSet(ps.Material.Al2O3)
                before = surface_top(domain, dimension)
                run_specialized(project, domain)
                counts = [int(level_set.getNumberOfPoints()) for level_set in domain.getLevelSets()]
                self.assertTrue(counts and all(count > 0 for count in counts), counts)
                self.assertGreater(surface_top(domain, dimension), before + 1e-6)

    def test_3d_trench_matches_a_direct_native_reference(self):
        project = geometric_project(3)
        parameters = project["steps"][0]["nativeParameters"]
        product = native_trench(3)
        product.duplicateTopLevelSet(ps.Material.Al2O3)
        run_specialized(project, product)
        reference = native_trench(3)
        reference.duplicateTopLevelSet(ps.Material.Al2O3)
        self.run_direct_native(reference, 3, parameters)
        product_signature = surface_signature(product, 3)
        reference_signature = surface_signature(reference, 3)
        self.assertEqual(len(product_signature[0]), len(reference_signature[0]))
        for left, right in zip(product_signature[0], reference_signature[0]):
            for a, b in zip(left, right):
                self.assertAlmostEqual(a, b, delta=1e-6)
        self.assertEqual(product_signature[1], reference_signature[1])

    def test_3d_geometry_run_is_deterministic(self):
        signatures = []
        for _ in range(2):
            project = geometric_project(3)
            domain = native_trench(3)
            domain.duplicateTopLevelSet(ps.Material.Al2O3)
            run_specialized(project, domain)
            signatures.append(surface_signature(domain, 3))
        self.assertEqual(signatures[0], signatures[1])

    def test_process_duration_does_not_scale_the_thickness(self):
        # The native geometric advection runs once; duration is recorded but
        # cannot change the distribution-driven thickness.
        heights = {}
        for duration in (0.02, 2.0):
            project = geometric_project(2)
            project["steps"][0]["duration"] = duration
            domain = native_plane(2)
            domain.duplicateTopLevelSet(ps.Material.Al2O3)
            run_specialized(project, domain)
            heights[duration] = surface_top(domain, 2)
        self.assertAlmostEqual(heights[0.02], heights[2.0], delta=1e-9)


def run_selective_direct(domain, dimension: int, factors, material_rates, rate111, rate100):
    if factors is None:
        model = ps.SelectiveEpitaxy(
            [(material_handle(name), float(value)) for name, value in sorted(material_rates.items())],
            float(rate111), float(rate100))
    else:
        model = ps.SelectiveEpitaxy([float(value) for value in factors],
                                    float(rate111), float(rate100))
        for name, value in sorted(material_rates.items()):
            model.setMaterialRate(material_handle(name), float(value))
    process = ps.Process(domain, model, 1.0)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = 0.25
    advection.spatialScheme = ps.ls.SpatialSchemeEnum.STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER
    process.setParameters(advection)
    process.apply()
    return domain


class NativeSelectiveEpitaxyNvFactors(unittest.TestCase):
    def test_omitted_and_explicit_native_default_are_identical_2d_and_3d(self):
        defaults = {2: [0.5, 1.0, 0.0], 3: [0.5, 0.0, 1.0]}
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                omitted_domain = native_plane(dimension)
                omitted_project = selective_project(dimension)
                run_specialized(omitted_project, omitted_domain)
                explicit_domain = native_plane(dimension)
                explicit_project = selective_project(dimension, nv_factors=defaults[dimension])
                _model, actual = run_specialized(explicit_project, explicit_domain)
                self.assertEqual(surface_signature(omitted_domain, dimension),
                                 surface_signature(explicit_domain, dimension))
                nv_actual = actual["nativeNvFactors"]
                self.assertEqual(nv_actual["requested"], defaults[dimension])
                self.assertEqual(nv_actual["effective"], defaults[dimension])
                self.assertEqual(nv_actual["nativeModule"], f"viennaps.d{dimension}")
                self.assertIsNone(nv_actual["nativeReadback"])
                self.assertEqual(nv_actual["materialRatesAppliedAfterConstruction"],
                                 {"Si": 0.5, "SiGe": 0.5})

    def test_nv_factors_can_stop_growth_along_the_dimension_vertical_axis(self):
        # 2D vertical axis is y (index 1); 3D vertical axis is z (index 2).
        for dimension, blocking in ((2, [0.5, 0.0, 1.0]), (3, [0.5, 1.0, 0.0])):
            with self.subTest(dimension=dimension):
                project = selective_project(dimension, nv_factors=blocking)
                domain = native_plane(dimension)
                before = surface_top(domain, dimension)
                run_specialized(project, domain)
                after = surface_top(domain, dimension)
                self.assertAlmostEqual(after, before, delta=1e-9)

    def test_omitted_run_grows_the_flat_surface(self):
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                project = selective_project(dimension)
                domain = native_plane(dimension)
                before = surface_top(domain, dimension)
                run_specialized(project, domain)
                after = surface_top(domain, dimension)
                self.assertAlmostEqual(after - before, 0.5, delta=0.02)

    def test_explicit_nv_factors_match_a_direct_native_reference(self):
        for dimension, factors in ((2, [1.0, 0.5, 0.0]), (3, [1.0, 0.0, 0.5])):
            with self.subTest(dimension=dimension):
                project = selective_project(dimension, nv_factors=factors)
                parameters = project["steps"][0]["nativeParameters"]
                product = native_plane(dimension)
                run_specialized(project, product)
                reference = native_plane(dimension)
                reference.duplicateTopLevelSet(ps.Material.SiGe)
                run_selective_direct(reference, dimension, factors, parameters["materialRates"],
                                     parameters["rate111"], parameters["rate100"])
                self.assertEqual(surface_signature(product, dimension),
                                 surface_signature(reference, dimension))

    def test_explicit_nv_factors_are_deterministic(self):
        for dimension, factors in ((2, [1.0, 0.5, 0.0]), (3, [1.0, 0.0, 0.5])):
            with self.subTest(dimension=dimension):
                signatures = []
                for _ in range(2):
                    project = selective_project(dimension, nv_factors=factors)
                    domain = native_plane(dimension)
                    run_specialized(project, domain)
                    signatures.append(surface_signature(domain, dimension))
                self.assertEqual(signatures[0], signatures[1])

    def test_omitted_request_hash_is_pinned_and_explicit_factors_change_it(self):
        pinned = {2: "169d3c63db0479ce5c1a1738b74dbb86c332d9c83840a1bc0f4d2a513fa86dc1",
                  3: "64df9a40467f709e06a39b4afb0616c4ba0fd79bc720600bea4f225710e53e5f"}
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                omitted = requested_input_hash(validate_project(selective_project(dimension))[0])
                self.assertEqual(omitted, pinned[dimension])
                explicit = requested_input_hash(validate_project(
                    selective_project(dimension, nv_factors=[0.5, 0.5, 0.5]))[0])
                self.assertNotEqual(omitted, explicit)


class NativeCoverageTolerance(unittest.TestCase):
    def test_omitted_default_is_the_historical_1e3_and_adds_no_metadata(self):
        project = plasma_project(2)
        parsed, _ = validate_project(project)
        self.assertNotIn("coverageTolerance", parsed.steps[0].nativeParameters)
        self.assertEqual(
            requested_input_hash(parsed),
            "f06dd19066dc3c41f283406dcfc3c0bdae54b1d2772aa0f19dc2dbf1ad7c2670")
        domain = native_stack(2, "reflective")
        domain.enableMetaData(ps.MetaDataLevel.FULL)
        _model, actual = run_specialized(project, domain)
        self.assertNotIn("nativeCoverageParameters", actual)
        # The worker still sends the historical 1e-3.
        self.assertEqual(domain.getMetaData()["CoverageTolerance"], [1e-3])

    def test_explicit_value_is_read_back_and_changes_the_run(self):
        signatures = {}
        readbacks = {}
        for tolerance in (0.0, 1e-3):
            with self.subTest(tolerance=tolerance):
                project = plasma_project(2)
                project["steps"][0]["nativeParameters"]["coverageTolerance"] = tolerance
                domain = native_stack(2, "reflective")
                domain.enableMetaData(ps.MetaDataLevel.FULL)
                _model, actual = run_specialized(project, domain)
                metadata = domain.getMetaData()
                self.assertEqual(metadata["CoverageTolerance"], [tolerance])
                nv = actual["nativeCoverageParameters"]
                self.assertEqual(nv["requested"], tolerance)
                self.assertEqual(nv["effective"], tolerance)
                self.assertEqual(nv["nativeReadback"]["CoverageTolerance"], [tolerance])
                signatures[tolerance] = surface_signature(domain, 2)
                readbacks[tolerance] = metadata["CoverageMaxIterations"][0]
        # tolerance 0.0 leaves maxIterations at its native default, so the
        # installed strategy applies its 10-iteration fallback.
        self.assertEqual(readbacks[0.0], 10.0)
        self.assertEqual(readbacks[1e-3], 4294967295.0)
        self.assertNotEqual(signatures[0.0], signatures[1e-3])

    def test_explicit_zero_is_accepted_and_negative_or_nonfinite_is_rejected(self):
        project = plasma_project(2)
        project["steps"][0]["nativeParameters"]["coverageTolerance"] = 0.0
        parsed, _ = validate_project(project)
        self.assertEqual(parsed.steps[0].nativeParameters["coverageTolerance"], 0.0)
        for value in (-1e-9, 1.0001, float("nan"), float("inf")):
            with self.subTest(value=value):
                project = plasma_project(2)
                project["steps"][0]["nativeParameters"]["coverageTolerance"] = value
                with self.assertRaises(ProjectInputError):
                    validate_project(project)


class SpecializedHashAndPersistence(unittest.TestCase):
    # FARADAY_3D_BASELINE_HASH was recorded on base 18abea0 in a clean archive:
    # this task changes no schema field, so an existing 3D project's
    # requested-input hash must be byte-identical. The 2D value is the same
    # canonical payload for a project the base rejected at validation, so it
    # pins the new surface without touching the 3D input shape.
    FARADAY_3D_BASELINE_HASH = "6d7666c44beb2a81c1fe3cfabf3281476e18748c7782ae25d6763dd27660d3fb"
    FARADAY_2D_HASH = "77f9e483bcdd397984262ab8d112904b67840ea5ba7f389fbe237cb7b9503326"

    def test_faraday_request_hashes_are_pinned_and_dimension_sensitive(self):
        with measured_engine():
            self.assertEqual(
                requested_input_hash(validate_project(faraday_project(3))[0]),
                self.FARADAY_3D_BASELINE_HASH)
            self.assertEqual(
                requested_input_hash(validate_project(faraday_project(2))[0]),
                self.FARADAY_2D_HASH)

    def test_native_parameters_change_the_request_hash(self):
        project = faraday_project(3)
        first = requested_input_hash(validate_project(project)[0])
        edited = copy.deepcopy(project)
        edited["steps"][0]["nativeParameters"]["cageAngle"] = 12.5
        self.assertNotEqual(first, requested_input_hash(validate_project(edited)[0]))

        project = geometric_project(2)
        first = requested_input_hash(validate_project(project)[0])
        edited = copy.deepcopy(project)
        edited["steps"][0]["nativeParameters"]["b"] = 0.2
        self.assertNotEqual(first, requested_input_hash(validate_project(edited)[0]))

    def test_projects_save_and_reopen_with_native_parameters(self):
        storage.initialize()
        for factory, dimension in ((faraday_project, 3), (geometric_project, 2)):
            with self.subTest(model=factory.__name__, dimension=dimension):
                normalized = validate_project(factory(dimension))[0].model_dump(mode="json", exclude_none=True)
                record = storage.create_project(normalized)
                reopened = storage.read_json(storage.project_path(record["id"]))["project"]
                self.assertEqual(reopened["steps"][0]["nativeModel"],
                                 normalized["steps"][0]["nativeModel"])
                self.assertEqual(reopened["steps"][0]["nativeParameters"],
                                 normalized["steps"][0]["nativeParameters"])
                parsed, _warnings = validate_project(reopened)
                self.assertEqual(parsed.steps[0].nativeModel, normalized["steps"][0]["nativeModel"])


class ModelSpecGuard(unittest.TestCase):
    def test_new_ids_are_looked_up_by_the_allowlist(self):
        self.assertEqual(model_spec("faradayCage")["nativeClass"], "FaradayCageEtching")
        self.assertEqual(model_spec("geometricTrenchDeposition")["nativeClass"],
                         "GeometricTrenchDeposition")
        self.assertIsNone(model_spec("FaradayCageEtching"))


class FaradayCpuReplay(unittest.IsolatedAsyncioTestCase):
    """A real experimental CPU run must remain replayable (codex checkpoint)."""

    async def asyncSetUp(self):
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)

    async def wait(self, run_id: str) -> dict:
        for _ in range(1200):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}:
                self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail("specialized CPU run did not finish")

    async def test_faraday_cage_cpu_run_replays(self):
        project = faraday_project(3)
        project["name"] = "FaradayCage CPU replay checkpoint"
        project["solver"]["enginePreference"] = "cpu"
        project["solver"]["timeoutSeconds"] = 60
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": project})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        await self.wait(run_id)
        envelope = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        replay = await asgi_request("/api/runs/replay", method="POST", payload=envelope)
        self.assertEqual(replay[0], 202, replay[2])
        await self.wait(json_body(replay)["runId"])

    @unittest.skipUnless(FARADAY_2D_ETCHES,
                         "ViennaPS 4.7.0 gates the 2D FaradayCageEtching no-op")
    async def test_faraday_cage_2d_cpu_run_replays(self):
        project = faraday_project(2)
        project["steps"][0]["nativeParameters"].update({"cageAngle": 45.0, "tiltAngle": 30.0})
        project["name"] = "FaradayCage 2D CPU replay checkpoint"
        project["solver"]["enginePreference"] = "cpu"
        project["solver"]["timeoutSeconds"] = 60
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": project})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        info = await self.wait(run_id)
        expected_hash = requested_input_hash(validate_project(project)[0])
        self.assertEqual(info["requestedInputHash"], expected_hash)
        manifest = json_body(await asgi_request(f"/api/runs/{run_id}"))
        native = manifest["actualExecution"][0]["actualParameters"]["nativeModel"]
        self.assertEqual(native["nativeParameterContainer"]["dimension"], 2)
        self.assertEqual(native["nativeParameterContainer"]["fluxEngine"], "CPU_DISK")
        envelope = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        replay = await asgi_request("/api/runs/replay", method="POST", payload=envelope)
        self.assertEqual(replay[0], 202, replay[2])
        replay_info = await self.wait(json_body(replay)["runId"])
        self.assertEqual(replay_info["requestedInputHash"], expected_hash)

    async def test_faraday_cage_2d_auto_is_rejected_before_the_worker(self):
        project = faraday_project(2)
        project["solver"]["enginePreference"] = "auto"
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": project})
        self.assertEqual(response[0], 422, response[2])
        payload = json_body(response)["detail"]
        if engine_version() == "4.6.2":
            self.assertTrue(any("CPU_DISK" in issue.get("message", "")
                                for issue in payload), payload)
        else:
            self.assertTrue(any("produces no etch" in issue.get("message", "")
                                for issue in payload), payload)

    async def test_faraday_energy_reversal_is_rejected_before_the_worker(self):
        # Exact codex repro: mean 100 / threshold 200 / sigma 500 grew the
        # surface on the pre-fix build. It must now be a 422.
        project = faraday_project(2)
        project["steps"][0]["nativeParameters"].update({
            "meanEnergy": 100.0, "thresholdEnergy": 200.0, "sigmaEnergy": 500.0})
        project["solver"]["enginePreference"] = "cpu"
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": project})
        self.assertEqual(response[0], 422, response[2])
        detail = json_body(response)["detail"]
        self.assertTrue(any(issue.get("path") == "steps[0].nativeParameters.thresholdEnergy"
                            and "less than meanEnergy" in issue.get("message", "")
                            for issue in detail), detail)

    async def test_faraday_string_energy_is_rejected_before_the_worker(self):
        # ProjectV7.nativeParameters is dict[str, Any]; a numeric string must be
        # a 422 instead of being coerced by the worker's float().
        project = faraday_project(2)
        project["steps"][0]["nativeParameters"].update({
            "meanEnergy": 100.0, "thresholdEnergy": "200", "sigmaEnergy": 500.0})
        project["solver"]["enginePreference"] = "cpu"
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": project})
        self.assertEqual(response[0], 422, response[2])
        detail = json_body(response)["detail"]
        self.assertTrue(any(issue.get("path") == "steps[0].nativeParameters.thresholdEnergy"
                            and "finite JSON number" in issue.get("message", "")
                            for issue in detail), detail)

    async def test_explicit_coverage_tolerance_cpu_run_replays(self):
        project = plasma_project(2)
        project["name"] = "Coverage tolerance CPU replay checkpoint"
        project["steps"][0]["nativeParameters"]["coverageTolerance"] = 1e-4
        project["solver"]["enginePreference"] = "cpu"
        project["solver"]["timeoutSeconds"] = 60
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": project})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        await self.wait(run_id)
        manifest = json_body(await asgi_request(f"/api/runs/{run_id}"))
        self.assertEqual(manifest["requestedInputHash"],
                         requested_input_hash(validate_project(project)[0]))
        envelope = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        replay = await asgi_request("/api/runs/replay", method="POST", payload=envelope)
        self.assertEqual(replay[0], 202, replay[2])
        replay_info = await self.wait(json_body(replay)["runId"])
        self.assertEqual(replay_info["requestedInputHash"],
                         requested_input_hash(validate_project(project)[0]))


if __name__ == "__main__":
    unittest.main()
