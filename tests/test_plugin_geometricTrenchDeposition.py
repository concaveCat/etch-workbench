"""Tests for the native v2 geometric-trench deposition plugin.

Every execution test builds a small 2D/3D Si plane or Si/Si3N4 trench, registers
the ``geometricTrenchDeposition``/``native`` plugin in a **local**
:class:`~process_models.v2.registry.ProcessRegistry`, and resolves an
:class:`~process_models.v2.resolver.ExecutionRequest` against a
:class:`~process_models.v2.engine.StaticEngineProvider` holding the test module
proxy. The shared plugin registry, ``load_registry()``, ``run_v2_step()`` and
``plugins.lock`` are never consulted: adding an in-tree plugin makes the
checked-in lock stale until it is regenerated, which is out of scope here.

The result is compared with a direct ``ps.GeometricTrenchDeposition`` +
``ps.Process`` reference through
:func:`worker.process_models_v2.compare_domains`. That reference duplicates the
top level set as the step's ``depositMaterial`` first, exactly like the plugin
and the stage-8 direct-reference deposit path, because the native model grows
the new level set rather than converting the substrate.

``GeometricTrenchDeposition`` is a ViennaLS geometric-advection distribution:
it has no flux, no rays, and its thickness does not scale with the process
duration. Both paths therefore configure advection only and pin ``CPU_DISK``;
the duration-invariance test proves the step duration is ignored. Only CPU
profiles are tested: the plugin advertises ``CPU_DISK`` alone.

No server or worker process is involved; the v2 state store writes into
``var/test-plugin-geometricTrenchDeposition/``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from typing import Any

import viennaps as ps
from pydantic import ValidationError

from backend.specialized_models import model_spec
from plugins.geometricTrenchDeposition import (
    GeometricTrenchDepositionNativeVariant,
    GeometricTrenchDepositionParams,
    GeometricTrenchDepositionProcessSpec,
    parse_step,
    register,
)
from process_models.v2.contract import ProcessContext, VariantRegistration
from process_models.v2.engine import (
    ComputePolicy,
    EngineCapabilities,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.engines.viennaps import configure_advection, material_handle
from process_models.v2.registry import ProcessRegistry
from process_models.v2.resolver import (
    ExecutionRequest,
    ExecutionResolutionError,
    resolve_execution,
)
from process_models.v2.state import LevelSetState, MaterialState, ProcessState, StepResult
from worker.process_models_v2 import compare_domains, material_order

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "var" / "test-plugin-geometricTrenchDeposition"
PLUGIN_TOML = ROOT / "plugins" / "geometricTrenchDeposition" / "plugin.toml"

GRID_DELTA = 0.2
X_EXTENT = 4.0
VIENNAPS_VERSION = str(ps.__version__)
ENGINE_FINGERPRINT = "test-geometricTrenchDeposition"
DEPOSIT_MATERIAL = "Al2O3"

#: ``backend.specialized_models`` defaults for ``geometricTrenchDeposition``.
CATALOG_DEFAULTS = {
    "trenchWidth": 1.0,
    "trenchDepth": 4.0,
    "depositionRate": 0.5,
    "bottomMed": 0.2,
    "a": 0.3,
    "b": 0.1,
    "n": 1.0,
}

#: Reach guard: max(a + b, bottomMed) = 0.6 <= 0.9.
EXPLICIT_PARAMETERS = {
    "trenchWidth": 1.2,
    "trenchDepth": 3.0,
    "depositionRate": 0.9,
    "bottomMed": 0.25,
    "a": 0.4,
    "b": 0.2,
    "n": 2.0,
}

FLAT_FIELDS = tuple(CATALOG_DEFAULTS)


def _solver() -> dict[str, Any]:
    return {
        "timeStepRatio": 0.25,
        "cpuThreads": 1,
        "enginePreference": "cpu",
        "advection": {
            "spatialScheme": "ENGQUIST_OSHER_1ST_ORDER",
            "temporalScheme": "FORWARD_EULER",
            "adaptiveTimeStepping": False,
            "adaptiveTimeStepSubdivisions": 20,
            "calculateIntermediateVelocities": False,
            "checkDissipation": True,
            "dissipationAlpha": 1.0,
            "ignoreVoids": False,
            "velocityOutput": False,
        },
    }


def plane_geometry() -> dict[str, Any]:
    return {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}


def trench_geometry() -> dict[str, Any]:
    return {
        "kind": "trench",
        "width": 1.0,
        "depth": 2.0,
        "taperAngle": 0.0,
        "maskHeight": 0.4,
        "maskTaperAngle": 0.0,
        "substrateMaterial": "Si",
    }


def geometric_step(
    *,
    duration: Any = 0.5,
    native_parameters: dict[str, Any] | None = None,
    deposit_material: Any = DEPOSIT_MATERIAL,
) -> dict[str, Any]:
    parameters = dict(CATALOG_DEFAULTS)
    parameters.update(native_parameters or {})
    return {
        "id": "step_geometricTrenchDeposition",
        "name": "Geometric trench",
        "enabled": True,
        "model": "specialized",
        "nativeModel": "geometricTrenchDeposition",
        "operation": "deposit",
        "depositMaterial": deposit_material,
        "duration": duration,
        "nativeParameters": parameters,
    }


def geometric_project(
    dimension: int = 2,
    *,
    geometry: str = "plane",
    grid_delta: float = GRID_DELTA,
    x_extent: float = X_EXTENT,
    native_parameters: dict[str, Any] | None = None,
    duration: Any = 0.5,
    deposit_material: Any = DEPOSIT_MATERIAL,
) -> dict[str, Any]:
    domain: dict[str, Any] = {"gridDelta": grid_delta, "xExtent": x_extent}
    if dimension == 2:
        domain["xBoundary"] = "reflective"
    else:
        domain["yExtent"] = x_extent
        domain["boundaryXY"] = "reflective"
    return {
        "schemaVersion": 7,
        "name": f"geometricTrenchDeposition plugin test {dimension}D {geometry}",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": plane_geometry() if geometry == "plane" else trench_geometry(),
        "steps": [
            geometric_step(
                duration=duration,
                native_parameters=native_parameters,
                deposit_material=deposit_material,
            )
        ],
        "solver": _solver(),
    }


def _new_domain(dimension: int, *, grid_delta: float = GRID_DELTA, x_extent: float = X_EXTENT) -> Any:
    ps.setDimension(dimension)
    if dimension == 2:
        return ps.Domain(
            gridDelta=grid_delta,
            xExtent=x_extent,
            boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
        )
    return ps.Domain(
        gridDelta=grid_delta,
        xExtent=x_extent,
        yExtent=x_extent,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )


def plane_domain(dimension: int = 2, *, grid_delta: float = GRID_DELTA, x_extent: float = X_EXTENT) -> Any:
    domain = _new_domain(dimension, grid_delta=grid_delta, x_extent=x_extent)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def trench_domain(dimension: int = 2, *, grid_delta: float = GRID_DELTA, x_extent: float = X_EXTENT) -> Any:
    domain = _new_domain(dimension, grid_delta=grid_delta, x_extent=x_extent)
    ps.MakeTrench(
        domain,
        trenchWidth=1.0,
        trenchDepth=2.0,
        trenchTaperAngle=0.0,
        maskHeight=0.4,
        maskTaperAngle=0.0,
        halfTrench=False,
        material=ps.Material.Si,
        maskMaterial=ps.Material.Si3N4,
    ).apply()
    return domain


def _top(domain: Any, dimension: int) -> float:
    mesh = domain.getSurfaceMesh(addInterfaces=True, minNodeDistanceFactor=0.01)
    axis = 1 if dimension == 2 else 2
    return max(float(node[axis]) for node in mesh.getNodes())


def _reference(
    ps_module: Any,
    project: dict[str, Any],
    domain: Any,
    *,
    kernel: str = "CPU_DISK",
    duration: float | None = None,
) -> Any:
    """Build the geometric-trench deposit path with no worker or plugin code.

    The top level set is duplicated as ``depositMaterial`` before
    ``ps.Process`` is built, because ``GeometricTrenchDeposition`` grows that
    new level set. Only advection is configured: the model has no flux.
    """
    step = project["steps"][0]
    values = step["nativeParameters"]
    domain.duplicateTopLevelSet(material_handle(ps_module, step["depositMaterial"]))
    model = ps_module.GeometricTrenchDeposition(*(float(values[name]) for name in FLAT_FIELDS))
    process = ps_module.Process(
        domain,
        model,
        float(step["duration"] if duration is None else duration),
    )
    process.setFluxEngineType(getattr(ps_module.FluxEngineType, kernel))
    advection, _actual = configure_advection(ps_module, project)
    process.setParameters(advection)
    process.apply()
    return domain


def _cpu_provider(handle: Any, version: str = VIENNAPS_VERSION) -> Any:
    return StaticEngineProvider(
        [
            StaticEngine(
                capabilities=EngineCapabilities(
                    engine_id="viennaps",
                    version=version,
                    kind="native",
                    backends=("cpu",),
                    kernels=("CPU_DISK",),
                ),
                handle=handle,
                fingerprint=ENGINE_FINGERPRINT,
            )
        ]
    )


class GeometricTrenchDepositionPluginTest(unittest.TestCase):
    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.Time.setUnit("s")
        self.work_dir = TEST_ROOT / self._testMethodName
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)
        self.ps = ps
        self.registry = ProcessRegistry()
        self.registration = register(self.registry)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)
        try:
            TEST_ROOT.rmdir()
        except OSError:
            pass

    # -- identity / parameters --------------------------------------------

    def test_manifest_and_identity_match_the_contract(self) -> None:
        manifest = tomllib.loads(PLUGIN_TOML.read_text(encoding="utf-8"))
        self.assertEqual(manifest["api"], 1)
        self.assertEqual(manifest["id"], "geometricTrenchDeposition")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["entrypoint"], "plugins.geometricTrenchDeposition:register")

        self.assertEqual(GeometricTrenchDepositionProcessSpec.id, "geometricTrenchDeposition")
        self.assertEqual(GeometricTrenchDepositionProcessSpec.version, "1.0.0")
        self.assertEqual(GeometricTrenchDepositionProcessSpec.params_model, GeometricTrenchDepositionParams)
        self.assertEqual(GeometricTrenchDepositionProcessSpec.dimensions, (2, 3))
        self.assertEqual(GeometricTrenchDepositionProcessSpec.operations, ("deposit",))
        self.assertEqual(GeometricTrenchDepositionProcessSpec.default_variant, "native")
        self.assertIs(GeometricTrenchDepositionProcessSpec.parse_step, parse_step)

        variant = GeometricTrenchDepositionNativeVariant()
        self.assertEqual(variant.variant_id, "native")
        self.assertEqual(variant.version, "1.0.0")
        self.assertEqual(variant.capabilities.dimensions, (2, 3))
        self.assertEqual(variant.capabilities.reads, frozenset({"geometry", "materials"}))
        self.assertEqual(variant.capabilities.writes, frozenset({"geometry", "materials"}))
        self.assertFalse(variant.capabilities.supports_masks)
        self.assertTrue(variant.capabilities.mutates_input)
        self.assertEqual(variant.capabilities.determinism, "strict")
        self.assertTrue(variant.capabilities.state_profile.allow_new_materials)
        self.assertEqual(variant.engine.kind, "native")
        self.assertEqual(variant.engine.engine_id, "viennaps")
        self.assertEqual(variant.engine.min_version, "4.6.2")
        profiles = {(profile.backend, profile.kernel) for profile in variant.engine.profiles}
        # This model has no flux engine and no verified GPU path, so CPU_DISK
        # is the only advertised profile (and the only one tested).
        self.assertEqual(profiles, {("cpu", "CPU_DISK")})
        self.assertIsNone(variant.implementation_hash)
        self.assertTrue(variant.implementation_files)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "geometricTrenchDeposition")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("geometricTrenchDeposition", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)
        self.assertEqual(self.registry.default_variant_id("geometricTrenchDeposition"), "native")

    def test_params_defaults_and_ranges_match_the_catalog(self) -> None:
        spec = model_spec("geometricTrenchDeposition")
        self.assertIsNotNone(spec)
        assert spec is not None
        fields = {field["key"]: field for field in spec["fields"]}
        self.assertEqual(set(fields), set(CATALOG_DEFAULTS))
        self.assertEqual(set(fields), set(FLAT_FIELDS))
        self.assertEqual(list(CATALOG_DEFAULTS), list(GeometricTrenchDepositionParams.model_fields)[1:8])

        params = GeometricTrenchDepositionParams(duration=1.0, depositMaterial=DEPOSIT_MATERIAL)
        for name, default in CATALOG_DEFAULTS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(params, name), default)
        self.assertEqual(params.depositMaterial, DEPOSIT_MATERIAL)
        self.assertEqual(params.flat_arguments(), tuple(CATALOG_DEFAULTS.values()))
        self.assertEqual(params.flat_values(), CATALOG_DEFAULTS)
        # Native reach at z=0 is a + b = 0.4, which the default rate 0.5 clears.
        self.assertEqual(params.reach_radius(), 0.4)
        self.assertIsNone(params.reach_guard_violation())
        self.assertEqual(
            params.reach_guard(),
            {
                "formula": "max(a + b, bottomMed)",
                "requiredRate": 0.4,
                "depositionRate": 0.5,
                "satisfied": True,
            },
        )

        # Every bound the catalog offers is accepted by the parameter model.
        for name, field in fields.items():
            for bound in ("minimum", "maximum"):
                with self.subTest(field=name, bound=bound):
                    GeometricTrenchDepositionParams(
                        duration=1.0,
                        depositMaterial=DEPOSIT_MATERIAL,
                        **{name: field[bound]},
                    )

        invalid = (
            {"duration": 0.0},
            {"duration": -1.0},
            {"duration": float("nan")},
            {"duration": float("inf")},
            {"trenchWidth": 0.0},
            {"trenchWidth": -0.1},
            {"trenchWidth": float("inf")},
            {"trenchWidth": float("nan")},
            {"trenchDepth": 0.0},
            {"trenchDepth": -1.0},
            {"depositionRate": 0.0},
            {"depositionRate": -1.0},
            {"depositionRate": float("inf")},
            {"bottomMed": -0.1},
            {"bottomMed": float("nan")},
            {"a": -0.1},
            {"b": -0.1},
            {"n": 0.5},
            {"n": 100.5},
            {"n": float("nan")},
            {"n": float("inf")},
            {"depositMaterial": ""},
            {"depositMaterial": "   "},
            {"unexpected": 1},
            {"trenchWidth": "1.0"},
            {"depositionRate": True},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    GeometricTrenchDepositionParams(
                        **{"duration": 1.0, "depositMaterial": DEPOSIT_MATERIAL, **overrides}
                    )

        with self.assertRaises(ValidationError):
            GeometricTrenchDepositionParams(duration=1.0)

        with self.assertRaises(ValidationError):
            params.duration = 2.0  # frozen

    def test_parse_step_maps_flat_fields_and_defaults(self) -> None:
        project = geometric_project()
        params = parse_step(project, project["steps"][0])
        self.assertIsInstance(params, GeometricTrenchDepositionParams)
        assert params is not None
        self.assertEqual(params.duration, 0.5)
        self.assertEqual(params.depositMaterial, DEPOSIT_MATERIAL)
        self.assertEqual(params.flat_values(), CATALOG_DEFAULTS)

        explicit = geometric_project(native_parameters=EXPLICIT_PARAMETERS)
        params = parse_step(explicit, explicit["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.flat_values(), EXPLICIT_PARAMETERS)
        self.assertEqual(params.flat_arguments(), tuple(EXPLICIT_PARAMETERS.values()))

        sparse = geometric_project(native_parameters={})
        sparse["steps"][0]["nativeParameters"] = {"depositionRate": 0.7}
        params = parse_step(sparse, sparse["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.depositionRate, 0.7)
        self.assertEqual(params.trenchWidth, 1.0)
        self.assertEqual(params.n, 1.0)

        claimed = self.registry.resolve_step(project, project["steps"][0])
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertIs(claimed[0], GeometricTrenchDepositionProcessSpec)
        self.assertIsInstance(claimed[1], GeometricTrenchDepositionParams)
        self.assertEqual(claimed[1].depositMaterial, DEPOSIT_MATERIAL)

    def test_parse_step_does_not_claim_other_steps(self) -> None:
        project = geometric_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {**step, "nativeParameters": dict(step["nativeParameters"])}
            copy.update(changes)
            return copy

        advanced = mutated()
        advanced["nativeParameters"]["advanced"] = {"precursors": {"rate": 0.1}}
        unknown = mutated()
        unknown["nativeParameters"]["coverageTolerance"] = 1e-3
        material_in_parameters = mutated()
        material_in_parameters["nativeParameters"]["depositMaterial"] = "SiO2"
        other_model = mutated(nativeModel="teosDeposition")
        not_specialized = mutated(model="isotropic")
        other_operation = mutated(operation="etch")
        gds = geometric_project(geometry="plane")
        gds["geometry"] = {"kind": "gds", "layers": []}
        one_dimensional = geometric_project()
        one_dimensional["dimension"] = 1

        for label, candidate_project, candidate in (
            ("advanced", project, advanced),
            ("coverageTolerance", project, unknown),
            ("depositMaterial in nativeParameters", project, material_in_parameters),
            ("teosDeposition", project, other_model),
            ("isotropic", project, not_specialized),
            ("etch", project, other_operation),
            ("gds geometry", gds, gds["steps"][0]),
            ("1D project", one_dimensional, one_dimensional["steps"][0]),
        ):
            with self.subTest(step=label):
                self.assertIsNone(parse_step(candidate_project, candidate))
                self.assertIsNone(self.registry.resolve_step(candidate_project, candidate))

    def test_parse_step_raises_for_invalid_flat_values(self) -> None:
        project = geometric_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {**step, "nativeParameters": dict(step["nativeParameters"])}
            copy.update(changes)
            return copy

        def with_parameters(**changes: Any) -> dict[str, Any]:
            return {**step, "nativeParameters": {**step["nativeParameters"], **changes}}

        invalid = (
            ("missing duration", {k: v for k, v in step.items() if k != "duration"}),
            ("zero duration", mutated(duration=0.0)),
            ("negative duration", mutated(duration=-1.0)),
            ("string duration", mutated(duration="1.0")),
            ("bool duration", mutated(duration=True)),
            ("nan duration", mutated(duration=float("nan"))),
            ("inf duration", mutated(duration=float("inf"))),
            ("missing depositMaterial", {k: v for k, v in step.items() if k != "depositMaterial"}),
            ("null depositMaterial", mutated(depositMaterial=None)),
            ("empty depositMaterial", mutated(depositMaterial="")),
            ("blank depositMaterial", mutated(depositMaterial="  ")),
            ("non-string depositMaterial", mutated(depositMaterial=7)),
            ("zero width", with_parameters(trenchWidth=0.0)),
            ("negative width", with_parameters(trenchWidth=-1.0)),
            ("zero depth", with_parameters(trenchDepth=0.0)),
            ("negative depth", with_parameters(trenchDepth=-1.0)),
            ("zero rate", with_parameters(depositionRate=0.0)),
            ("negative rate", with_parameters(depositionRate=-1.0)),
            ("inf rate", with_parameters(depositionRate=float("inf"))),
            ("negative bottomMed", with_parameters(bottomMed=-0.1)),
            ("negative a", with_parameters(a=-0.1)),
            ("negative b", with_parameters(b=-0.1)),
            ("n below one", with_parameters(n=0.5)),
            ("n above hundred", with_parameters(n=101.0)),
            ("nan n", with_parameters(n=float("nan"))),
            ("bool rate", with_parameters(depositionRate=True)),
            ("string width", with_parameters(trenchWidth="1.0")),
            ("nan depth", with_parameters(trenchDepth=float("nan"))),
            ("parameters type", {**step, "nativeParameters": [1, 2, 3]}),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step(project, candidate)

    def test_parse_step_enforces_the_reach_guard(self) -> None:
        project = geometric_project()
        step = project["steps"][0]

        def with_parameters(**changes: Any) -> dict[str, Any]:
            return {**step, "nativeParameters": {**step["nativeParameters"], **changes}}

        # Default radius is a + b = 0.4; 0.3 is below it.
        for overrides in (
            {"depositionRate": 0.3},
            {"a": 0.1, "b": 2.0, "bottomMed": 0.1, "depositionRate": 0.05},
            {"a": 0.0, "b": 0.0, "bottomMed": 0.5, "depositionRate": 0.49},
        ):
            with self.subTest(nativeParameters=overrides):
                with self.assertRaises(ValueError) as raised:
                    parse_step(project, with_parameters(**overrides))
                self.assertIn("native radius", str(raised.exception))

        # Exactly at the radius is accepted: the native window is inclusive.
        accepted = parse_step(project, with_parameters(depositionRate=0.4))
        self.assertIsNotNone(accepted)
        assert accepted is not None
        self.assertEqual(accepted.depositionRate, 0.4)

    def test_validate_flags_reach_guard_and_gds(self) -> None:
        variant = GeometricTrenchDepositionNativeVariant()
        project = geometric_project()
        ctx = ProcessContext(dimension=2, grid_delta=GRID_DELTA, project=project)
        good = GeometricTrenchDepositionParams(duration=1.0, depositMaterial=DEPOSIT_MATERIAL)
        self.assertEqual(variant.validate(ctx, good), [])

        unsafe = GeometricTrenchDepositionParams(
            duration=1.0,
            depositMaterial=DEPOSIT_MATERIAL,
            depositionRate=0.3,
        )
        issues = variant.validate(ctx, unsafe)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].path, "depositionRate")
        self.assertEqual(issues[0].severity, "error")
        self.assertIn("native radius", issues[0].message)

        gds_project = geometric_project()
        gds_project["geometry"] = {"kind": "gds", "layers": []}
        gds_ctx = ProcessContext(dimension=2, grid_delta=GRID_DELTA, project=gds_project)
        gds_issues = variant.validate(gds_ctx, good)
        self.assertEqual(len(gds_issues), 1)
        self.assertEqual(gds_issues[0].path, "geometry")
        self.assertIn("GDS", gds_issues[0].message)

    # -- execution ---------------------------------------------------------

    def _run(self, project: dict[str, Any], domain: Any) -> Any:
        """Register, resolve, and execute the plugin without the shared lock."""
        dimension = int(project["dimension"])
        step = project["steps"][0]
        params = parse_step(project, step)
        self.assertIsNotNone(params)
        state = self._state(project, domain)
        context = ProcessContext(
            dimension=dimension,
            grid_delta=float(project["domain"]["gridDelta"]),
            project=project,
            step=step,
        )
        plan = resolve_execution(
            ExecutionRequest(
                process_id="geometricTrenchDeposition",
                variant_id="native",
                params=params,
                context=context,
                state=state,
                compute=ComputePolicy(backend="cpu", kernel="CPU_DISK"),
            ),
            self.registry,
            _cpu_provider(self.ps),
        )
        return plan.run()

    def _state(self, project: dict[str, Any], domain: Any) -> ProcessState:
        dimension = int(project["dimension"])
        return ProcessState(
            geometry=LevelSetState(
                handle=domain,
                dimension=dimension,
                grid_delta=float(project["domain"]["gridDelta"]),
            ),
            materials=MaterialState(
                order=material_order(domain),
                names={int(ps.Material.Si): "Si"},
            ),
        )

    def _assert_parity(self, reference: Any, candidate: Any, dimension: int) -> dict[str, Any]:
        result = compare_domains(reference, candidate, dimension)
        self.assertTrue(result["matched"], result["reasons"])
        self.assertEqual(result["oldDigest"], result["newDigest"])
        self.assertEqual(result["oldMaterialMap"], result["newMaterialMap"])
        self.assertEqual(result["oldMaterialMap"], list(material_order(candidate)))
        self.assertGreater(result["oldPointCount"], 0)
        self.assertEqual(result["oldPointCount"], result["newPointCount"])
        return result

    def _assert_deposit_name(self, materials: MaterialState, name: str = DEPOSIT_MATERIAL) -> None:
        deposit_id = int(ps.Material.Al2O3)
        self.assertEqual(materials.name_for(deposit_id), name)
        self.assertEqual(materials.id_for(name), deposit_id)
        self.assertEqual(materials.name_for(int(ps.Material.Si)), "Si")

    def test_two_dimensional_plane_matches_the_native_reference(self) -> None:
        project = geometric_project(2, geometry="plane", duration=0.5)
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        after = _top(new_domain, 2)
        # radius at z=0 is a + b = 0.4 um; the substrate is flat at z=0.
        self.assertAlmostEqual(after - before, 0.4, delta=1e-6)

        delta = outcome.delta
        self.assertIsInstance(outcome.result, StepResult)
        self.assertIs(delta, outcome.result.delta)
        self.assertIsInstance(delta.geometry, LevelSetState)
        self.assertIs(delta.geometry.handle, new_domain)
        self.assertEqual(delta.geometry.dimension, 2)
        self.assertEqual(delta.geometry.grid_delta, GRID_DELTA)
        self.assertIsInstance(delta.materials, MaterialState)
        order = material_order(new_domain)
        self.assertEqual(delta.materials.order, order)
        self.assertEqual(order, (int(ps.Material.Si), int(ps.Material.Al2O3)))
        self._assert_deposit_name(delta.materials)
        self.assertIs(outcome.state.geometry, delta.geometry)
        self.assertEqual(outcome.state.materials, delta.materials)
        self.assertEqual(outcome.result.outputs.metrology, ())
        self.assertEqual(outcome.result.warnings, [])

        metadata = outcome.metadata
        self.assertEqual(metadata["nativeClass"], "ps.GeometricTrenchDeposition")
        self.assertEqual(metadata["nativeConstructor"], "seven-argument constructor")
        self.assertEqual(metadata["operation"], "deposit")
        self.assertEqual(metadata["depositMaterial"], DEPOSIT_MATERIAL)
        self.assertTrue(metadata["duplicateTopLevelSet"])
        self.assertEqual(metadata["arguments"], CATALOG_DEFAULTS)
        self.assertEqual(metadata["duration"], 0.5)
        self.assertTrue(metadata["durationIgnored"])
        self.assertFalse(metadata["durationScalesThickness"])
        self.assertFalse(metadata["trenchWidthUsedByNativeKernel"])
        self.assertEqual(
            metadata["reachGuard"],
            {
                "formula": "max(a + b, bottomMed)",
                "requiredRate": 0.4,
                "depositionRate": 0.5,
                "satisfied": True,
            },
        )
        self.assertIn("absolute z", metadata["depthReference"])
        self.assertEqual(metadata["timeStepRatio"], 0.25)
        self.assertEqual(metadata["fluxEngine"], "CPU_DISK")
        self.assertEqual(metadata["executionProfile"]["kernel"], "CPU_DISK")
        self.assertIsNone(metadata["rayTracing"])
        self.assertEqual(metadata["dimension"], 2)
        self.assertEqual(metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(metadata["materialMap"], list(order))
        self.assertEqual(metadata["levelSetCount"], 2)

        identity = outcome.plan.identity_json()
        self.assertEqual(identity["processId"], "geometricTrenchDeposition")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineKernel"], "CPU_DISK")
        self.assertEqual(identity["engineFingerprint"], ENGINE_FINGERPRINT)

    def test_two_dimensional_trench_matches_the_native_reference(self) -> None:
        project = geometric_project(
            2,
            geometry="trench",
            native_parameters=EXPLICIT_PARAMETERS,
            duration=0.5,
        )
        reference = trench_domain(2)
        candidate = trench_domain(2)
        before = _top(candidate, 2)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertGreater(_top(new_domain, 2), before)
        self.assertEqual(
            outcome.metadata["arguments"],
            {name: float(value) for name, value in EXPLICIT_PARAMETERS.items()},
        )
        self.assertEqual(outcome.metadata["reachGuard"]["formula"], "max(a + b, bottomMed)")
        self.assertAlmostEqual(outcome.metadata["reachGuard"]["requiredRate"], 0.6)
        self.assertEqual(outcome.metadata["reachGuard"]["depositionRate"], 0.9)
        self.assertTrue(outcome.metadata["reachGuard"]["satisfied"])

        delta = outcome.delta
        order = material_order(new_domain)
        self.assertEqual(delta.materials.order, order)
        # Si substrate, Si3N4 mask, and the duplicate deposit level set.
        self.assertEqual(
            order,
            (int(ps.Material.Si3N4), int(ps.Material.Si), int(ps.Material.Al2O3)),
        )
        self.assertEqual(outcome.metadata["levelSetCount"], 3)
        self.assertEqual(outcome.metadata["materialMap"], list(order))
        self._assert_deposit_name(delta.materials)
        self.assertEqual(delta.materials.name_for(int(ps.Material.Si3N4)), "Si3N4")

    def test_three_dimensional_plane_smoke(self) -> None:
        grid_delta = 0.4
        project = geometric_project(3, geometry="plane", grid_delta=grid_delta, x_extent=2.0, duration=0.2)
        reference = plane_domain(3, grid_delta=grid_delta, x_extent=2.0)
        candidate = plane_domain(3, grid_delta=grid_delta, x_extent=2.0)
        before = _top(candidate, 3)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 3)
        self.assertAlmostEqual(_top(new_domain, 3) - before, 0.4, delta=1e-6)
        self.assertEqual(outcome.delta.geometry.dimension, 3)
        self.assertEqual(outcome.delta.geometry.grid_delta, grid_delta)
        self.assertEqual(outcome.delta.materials.order, material_order(new_domain))
        self._assert_deposit_name(outcome.delta.materials)
        self.assertEqual(outcome.state.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        self.assertEqual(outcome.metadata["gridDelta"], grid_delta)
        self.assertEqual(outcome.metadata["levelSetCount"], 2)

    def test_duration_is_ignored(self) -> None:
        short = geometric_project(2, geometry="plane", duration=0.5)
        long = geometric_project(2, geometry="plane", duration=2.0)

        short_outcome = self._run(short, plane_domain(2))
        long_outcome = self._run(long, plane_domain(2))

        # The native distribution performs one geometric advection per
        # Process.apply(), so the process duration neither scales nor repeats it.
        result = compare_domains(
            short_outcome.state.geometry.handle,
            long_outcome.state.geometry.handle,
            2,
        )
        self.assertTrue(result["matched"], result["reasons"])
        self.assertEqual(result["oldDigest"], result["newDigest"])
        self.assertEqual(result["oldPointCount"], result["newPointCount"])
        self.assertAlmostEqual(_top(short_outcome.state.geometry.handle, 2), 0.4, delta=1e-6)
        self.assertAlmostEqual(_top(long_outcome.state.geometry.handle, 2), 0.4, delta=1e-6)

        self.assertEqual(short_outcome.metadata["duration"], 0.5)
        self.assertEqual(long_outcome.metadata["duration"], 2.0)
        self.assertTrue(short_outcome.metadata["durationIgnored"])
        self.assertTrue(long_outcome.metadata["durationIgnored"])

        # A direct reference that passes the native process a zero duration
        # produces the same surface: the plugin records the requested duration
        # without letting it change the result.
        reference = _reference(self.ps, short, plane_domain(2), duration=0.0)
        zero_result = compare_domains(reference, short_outcome.state.geometry.handle, 2)
        self.assertTrue(zero_result["matched"], zero_result["reasons"])

    def test_reach_guard_is_rejected_before_the_native_call(self) -> None:
        project = geometric_project(2, geometry="plane")
        unsafe = GeometricTrenchDepositionParams(
            duration=1.0,
            depositMaterial=DEPOSIT_MATERIAL,
            depositionRate=0.3,
        )
        context = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=project,
            step=project["steps"][0],
        )

        # The resolver rejects hand-built params that bypassed parse_step.
        state = self._state(project, plane_domain(2))
        with self.assertRaises(ExecutionResolutionError) as raised:
            resolve_execution(
                ExecutionRequest(
                    process_id="geometricTrenchDeposition",
                    variant_id="native",
                    params=unsafe,
                    context=context,
                    state=state,
                    compute=ComputePolicy(backend="cpu", kernel="CPU_DISK"),
                ),
                self.registry,
                _cpu_provider(self.ps),
            )
        self.assertIn("native radius", str(raised.exception))

        # execute() re-checks before duplicateTopLevelSet/ps.Process, so the
        # caller's domain is untouched and no native model is constructed.
        domain = plane_domain(2)
        self.assertEqual(domain.getNumberOfLevelSets(), 1)
        provider = _cpu_provider(self.ps)
        variant = GeometricTrenchDepositionNativeVariant()
        with provider.acquire(variant.engine, ComputePolicy(backend="cpu", kernel="CPU_DISK")) as lease:
            with self.assertRaises(RuntimeError) as raised:
                variant.execute(self._state(project, domain), unsafe, context, lease)
        self.assertIn("native radius", str(raised.exception))
        self.assertEqual(domain.getNumberOfLevelSets(), 1)

    # -- import isolation --------------------------------------------------

    def test_import_does_not_pull_in_the_worker_or_backend(self) -> None:
        code = (
            "import sys\n"
            "import plugins.geometricTrenchDeposition\n"
            "leaked = sorted(\n"
            "    name for name in sys.modules\n"
            "    if name == 'worker' or name.startswith('worker.')\n"
            "    or name == 'backend' or name.startswith('backend.')\n"
            ")\n"
            "print(leaked)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "[]", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
