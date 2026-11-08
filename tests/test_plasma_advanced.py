"""Strict ``nativeParameters.advanced`` support for the four plasma models.

The installed 4.6.2 bindings expose ``PlasmaEtchingParameters`` overloads for
SF6O2Etching/HBrO2Etching/SF6C4F8Etching and ``CF4O2Parameters`` for
CF4O2Etching. The workbench previously used only the flat float overloads, so
the nested Ions/Mask/Passivation/Polymer/Substrate (plus CF4O2 Si/SiGe and
gamma_*) families were unreachable.

These tests pin the contract:

* omitting ``advanced`` keeps the historical request hashes and native defaults;
* an empty ``advanced`` object still exercises the parameter-object overload;
* the catalog schema is exactly the subset the native CPU models consume:
  inert SiGe fields, the unused ``Substrate.B_ie``, and SF6C4F8's absent
  passivation family are rejected rather than documented as working;
* advanced defaults for flat-shared fields equal the flat catalog defaults;
* model-specific native defaults (HBrO2 Substrate, per-material maps) survive;
* 2D and 3D golden runs for all four models match an independent native
  reference within 1e-6 um;
* unknown keys, cross-model/inert blocks, wrong types, huge integers,
  out-of-range values and materials are rejected before the worker starts.
"""
from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import viennaps as ps  # noqa: E402

from backend import storage  # noqa: E402
from backend.hashing import requested_input_hash  # noqa: E402
from backend.main import app  # noqa: E402
from backend.materials import material_catalog  # noqa: E402
from backend.specialized_models import advanced_spec, model_spec  # noqa: E402
from backend.validation import ProjectInputError, validate_project  # noqa: E402
from stage8_project_factory import make_project  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from worker.specialized import build_specialized_model  # noqa: E402

PLASMA_MODELS = ("sf6o2", "cf4o2", "hbro2", "sf6c4f8")

# Request hashes computed on the merge base (d2d81a0) before ``advanced`` was
# accepted. They must never change when the optional object is omitted.
BASE_REQUEST_HASHES = {
    "sf6o2": {"2": "f06dd19066dc3c41f283406dcfc3c0bdae54b1d2772aa0f19dc2dbf1ad7c2670",
              "3": "343958f3456b764bab861058cf4b45f631ac81bf863d4d7b1c946222c1219daf"},
    "cf4o2": {"2": "fda6f3fdfe1ea5fdd41c01b16109f2ebbc3863e732883a481316ab356c26e6ae",
              "3": "f588f868298a41a28e854e04143f0de65110fda1a259a620b1ae10aef7d544e9"},
    "hbro2": {"2": "6efc908d90ea789a1487871259401a426bc409f79c3197fe34edb6982e44430a",
              "3": "ea4f559b824d09289f3a01b96d86212d59e6a55955b7dea4cb07894146827fc3"},
    "sf6c4f8": {"2": "194b2f67abe33bfacdc861443e707ee85c4b2ea726a6706b4f7eb65218a4a308",
                "3": "609e1beafba3767f4aa895f053a575ed5b16d0cf5b347b250ad055171df6012b"},
}

# Exact native-consumption audit for the installed CPU models.
INERT_BLOCKS = {"sf6c4f8": {"Passivation"}}
INERT_SCALARS = {"sf6c4f8": {"passivationFlux"}}
INERT_MAPS = {"sf6c4f8": {"beta_P"}}
INERT_BLOCK_FIELDS = {
    ("sf6o2", "substrate"): {"B_ie"},
    ("hbro2", "substrate"): {"B_ie"},
    ("sf6c4f8", "substrate"): {"B_ie"},
    ("cf4o2", "siGe"): {"A_ie", "A_sp", "Eth_ie", "Eth_sp", "x"},
}
EXPECTED_REJECTED = {
    "sf6c4f8": {"passivationFlux"},
}
# Native top-level names already set by a flat field but still exposed as an
# advanced scalar so clients can override the native spelling directly.
EXPOSED_FLAT_ALIASES = {
    "sf6o2": {"passivationFlux"},
    "hbro2": {"passivationFlux"},
    "cf4o2": set(),
    "sf6c4f8": set(),
}


def plasma_project(model_id: str, dimension: int = 2, advanced: dict | None = None,
                   flat_overrides: dict | None = None) -> dict:
    project = make_project(model_id, dimension)
    project["steps"][0]["duration"] = 0.1
    if flat_overrides:
        project["steps"][0]["nativeParameters"].update(copy.deepcopy(flat_overrides))
    if advanced is not None:
        project["steps"][0]["nativeParameters"]["advanced"] = copy.deepcopy(advanced)
    return project


def sige_project(dimension: int, advanced: dict, flat_overrides: dict) -> dict:
    project = plasma_project("cf4o2", dimension, advanced, flat_overrides)
    project["geometry"] = {
        "kind": "stack", "substrateMaterial": "Si", "baseHeight": 0.0,
        "layers": [{"id": "sige", "name": "SiGe", "material": "SiGe", "thickness": 0.4, "role": "film"}],
        "pattern": {"kind": "none"},
    }
    return project


def native_handle(name: str):
    return getattr(ps.Material, name)


def native_plane(dimension: int, top_material: str | None = "SiO2"):
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY
    domain = (ps.Domain(0.2, 4.0, boundary=boundary) if dimension == 2
              else ps.Domain(0.2, 4.0, 4.0, boundary))
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    if top_material is not None:
        ps.MakePlane(domain, height=0.4, material=getattr(ps.Material, top_material),
                     addToExisting=True).apply()
    return domain


def surface_top(domain, dimension: int) -> float:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    vertical = 1 if dimension == 2 else 2
    return max(float(point[vertical]) for point in mesh.getNodes())


def coverage_parameters():
    coverage = ps.CoverageParameters()
    coverage.tolerance = 1e-3
    return coverage


def run_model(model, duration: float, extra: list, dimension: int,
              top_material: str | None) -> float:
    domain = native_plane(dimension, top_material)
    process = ps.Process(domain, model, duration)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    rays = ps.RayTracingParameters()
    rays.raysPerPoint = 32
    rays.rngSeed = 271828
    rays.useRandomSeeds = False
    process.setParameters(rays)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = 0.25
    process.setParameters(advection)
    for item in extra:
        process.setParameters(item)
    process.apply()
    return surface_top(domain, dimension)


# Arguments that select the installed flat overload's own native defaults.
# They are parameter-class defaults, not a project's edited values.
_NATIVE_FLAT_DEFAULTS = {
    "sf6o2": (12.0, 1800.0, 100.0, 100.0, 10.0, 500.0, 3.0, -1.7976931348623157e308),
    "hbro2": (12.0, 1800.0, 100.0, 100.0, 10.0, 500.0, 3.0, -1.7976931348623157e308),
    "sf6c4f8": (12.0, 1800.0, 100.0, 10.0, 300.0, -1.7976931348623157e308),
    "cf4o2": (12.0, 1800.0, 100.0, 100.0, 100.0, 10.0, 500.0, 3.0, 3.0, -1.7976931348623157e308),
}


def native_default_parameters(model_id: str):
    """A flat model built with the binding's own defaults, plus its params."""
    ps.setDimension(2)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    model = getattr(ps, model_spec(model_id)["nativeClass"])(*_NATIVE_FLAT_DEFAULTS[model_id])
    return model, model.getParameters()


def native_reference(model_id: str, project: dict, dimension: int, mutate):
    """Build the same parameter object directly, without worker code."""
    ps.setDimension(dimension)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    values = project["steps"][0]["nativeParameters"]
    keys = [field["key"] for field in model_spec(model_id)["fields"]]
    native_class = getattr(ps, model_spec(model_id)["nativeClass"])
    flat_model = native_class(*(float(values[key]) for key in keys))
    parameters = flat_model.getParameters()
    mutate(parameters)
    return native_class(parameters), flat_model


def material_ids() -> dict[str, int]:
    return {row["name"]: int(row["id"]) for row in material_catalog()["materials"]}


def build(project: dict, dimension: int = 2, top_material: str | None = "SiO2"):
    parsed, _warnings = validate_project(project, kind="simulate")
    normalized = parsed.model_dump(mode="json", exclude_none=True)
    domain = native_plane(dimension, top_material)
    model, extra, actual, duration = build_specialized_model(
        ps, domain, normalized, normalized["steps"][0], native_handle, material_ids())
    return model, extra, actual, duration


def native_members(model_id: str):
    spec = advanced_spec(model_id)
    instance = getattr(ps, spec["parameterClass"])()
    blocks = {name for name in dir(instance)
              if type(getattr(instance, name)).__name__.startswith(spec["parameterClass"])}
    scalars = {name for name in dir(instance) if not name.startswith("_")
               and isinstance(getattr(instance, name), (float, bool))}
    maps = {name for name in dir(instance)
            if type(getattr(instance, name)).__name__ == "MaterialValueMap"}
    return instance, blocks, scalars, maps


class AdvancedSchemaContract(unittest.TestCase):
    def test_catalog_exposes_exactly_the_native_consumed_families(self):
        for model_id in PLASMA_MODELS:
            with self.subTest(model=model_id):
                spec = advanced_spec(model_id)
                instance, native_blocks, native_scalars, native_maps = native_members(model_id)
                flat_top_level = {path for path in spec["flatMapping"].values() if "." not in path}
                self.assertEqual({block["native"] for block in spec["blocks"].values()},
                                 native_blocks - INERT_BLOCKS.get(model_id, set()))
                self.assertEqual({field["key"] for field in spec["scalars"]},
                                 (native_scalars - flat_top_level - INERT_SCALARS.get(model_id, set()))
                                 | EXPOSED_FLAT_ALIASES[model_id])
                self.assertEqual({row["key"] for row in spec["maps"]},
                                 native_maps - INERT_MAPS.get(model_id, set()))
                self.assertEqual(set(spec["flatMapping"]),
                                 {field["key"] for field in model_spec(model_id)["fields"]})
                self.assertEqual(set(spec.get("rejectedKeys", {})), EXPECTED_REJECTED.get(model_id, set()))

                for name, block in spec["blocks"].items():
                    native = getattr(instance, block["native"])
                    native_fields = {field for field in dir(native) if not field.startswith("_")
                                     and isinstance(getattr(native, field), (float, bool))}
                    expected = native_fields - INERT_BLOCK_FIELDS.get((model_id, name), set())
                    self.assertEqual({field["key"] for field in block["fields"]}, expected,
                                     f"{model_id}.{name}")
                    for field in block["fields"]:
                        self.assertIsInstance(getattr(native, field["key"]),
                                              bool if field["kind"] == "boolean" else float)

                for row in spec["maps"]:
                    self.assertEqual(row["minimum"], 0.0)
                    self.assertEqual(row["maximum"], 1.0 if row["key"].startswith(("beta_", "gamma_")) else 1_000_000.0)

    def test_catalog_defaults_match_the_effective_flat_defaults(self):
        for model_id in PLASMA_MODELS:
            with self.subTest(model=model_id):
                spec = advanced_spec(model_id)
                flat_values = make_project(model_id)["steps"][0]["nativeParameters"]
                owner, native_params = native_default_parameters(model_id)
                for name, block in spec["blocks"].items():
                    native = getattr(native_params, block["native"])
                    for field in block["fields"]:
                        if field["defaultSource"] == "flat":
                            self.assertEqual(field["default"], flat_values[field["flatKey"]],
                                             f"{model_id}.{name}.{field['key']}")
                        else:
                            self.assertEqual(field["default"], getattr(native, field["key"]),
                                             f"{model_id}.{name}.{field['key']}")
                for field in spec["scalars"]:
                    if field["defaultSource"] == "flat":
                        self.assertEqual(field["default"], flat_values[field["flatKey"]],
                                         f"{model_id}.{field['key']}")
                    else:
                        self.assertEqual(field["default"], getattr(native_params, field["key"]),
                                         f"{model_id}.{field['key']}")
                for row in spec["maps"]:
                    self.assertEqual(row["default"], getattr(native_params, row["native"]).getDefault(),
                                     f"{model_id}.{row['key']}.default")
                self.assertEqual(type(owner).__name__, model_spec(model_id)["nativeClass"])

    def test_flat_shared_and_native_only_defaults_are_labelled(self):
        sf6o2 = {field["key"]: field for field in advanced_spec("sf6o2")["blocks"]["ions"]["fields"]}
        self.assertEqual((sf6o2["meanEnergy"]["default"], sf6o2["meanEnergy"]["defaultSource"],
                          sf6o2["meanEnergy"].get("flatKey")), (100, "flat", "meanIonEnergy"))
        self.assertEqual((sf6o2["exponent"]["default"], sf6o2["exponent"]["flatKey"]), (100, "ionExponent"))
        self.assertEqual((sf6o2["n_l"]["default"], sf6o2["n_l"]["defaultSource"]),
                         (10.0, "nativeParameterClass"))
        passivation = {field["key"]: field for field in advanced_spec("sf6o2")["blocks"]["passivation"]["fields"]}
        self.assertEqual((passivation["A_ie"]["default"], passivation["A_ie"]["flatKey"]),
                         (3, "oxySputterYield"))
        self.assertEqual({field["key"]: field["default"] for field in advanced_spec("sf6o2")["scalars"]},
                         {"passivationFlux": 0.2})
        self.assertEqual({field["key"]: field["default"] for field in advanced_spec("hbro2")["scalars"]},
                         {"passivationFlux": 0.2})
        cf4o2 = advanced_spec("cf4o2")
        self.assertEqual({field["key"]: (field["default"], field.get("flatKey"))
                          for field in cf4o2["blocks"]["passivation"]["fields"]},
                         {"A_C_ie": (3, "polySputterYield"), "A_O_ie": (3, "oxySputterYield"),
                          "Eth_C_ie": (10.0, None), "Eth_O_ie": (10.0, None)})
        self.assertEqual({field["key"]: field["default"] for field in cf4o2["blocks"]["ions"]["fields"]}["exponent"],
                         100)

    def test_inert_native_members_are_not_exposed(self):
        sf6c4f8 = advanced_spec("sf6c4f8")
        self.assertNotIn("passivation", sf6c4f8["blocks"])
        self.assertNotIn("passivationFlux", {field["key"] for field in sf6c4f8["scalars"]})
        self.assertNotIn("beta_P", {row["key"] for row in sf6c4f8["maps"]})
        self.assertIn("SF6C4F8Etching has no passivation particle",
                      sf6c4f8["rejectedKeys"]["passivationFlux"])
        cf4o2_sige = {field["key"] for field in advanced_spec("cf4o2")["blocks"]["siGe"]["fields"]}
        self.assertEqual(cf4o2_sige, {"beta_sigma", "k_sigma", "rho"})
        for model_id in ("sf6o2", "hbro2", "sf6c4f8"):
            substrate = advanced_spec(model_id)["blocks"]["substrate"]["fields"]
            self.assertNotIn("B_ie", {field["key"] for field in substrate})
        self.assertEqual(advanced_spec("sf6c4f8")["blocks"]["ions"]["fields"][2]["default"], 300)


class AdvancedHashStability(unittest.TestCase):
    def test_omitting_advanced_keeps_the_base_request_hashes(self):
        for model_id in PLASMA_MODELS:
            for dimension in (2, 3):
                with self.subTest(model=model_id, dimension=dimension):
                    parsed, _warnings = validate_project(make_project(model_id, dimension))
                    self.assertEqual(requested_input_hash(parsed), BASE_REQUEST_HASHES[model_id][str(dimension)])

    def test_an_advanced_value_changes_the_request_hash(self):
        for model_id in PLASMA_MODELS:
            with self.subTest(model=model_id):
                first = requested_input_hash(validate_project(plasma_project(model_id))[0])
                changed = requested_input_hash(validate_project(
                    plasma_project(model_id, advanced={"ions": {"meanEnergy": 90}}))[0])
                self.assertNotEqual(first, changed)


class AdvancedConstructionPath(unittest.TestCase):
    def materialized_advanced(self, model_id: str) -> dict:
        """Build the object a client gets by filling every published default."""
        spec = advanced_spec(model_id)
        advanced: dict = {}
        for name, block in spec["blocks"].items():
            advanced[name] = {field["key"]: field["default"] for field in block["fields"]}
        for field in spec["scalars"]:
            advanced[field["key"]] = field["default"]
        for row in spec["maps"]:
            advanced[row["key"]] = {"default": row["default"]}
        return advanced

    def test_materialising_catalog_defaults_keeps_the_effective_values(self):
        for dimension in (2, 3):
            for model_id in PLASMA_MODELS:
                with self.subTest(model=model_id, dimension=dimension):
                    _model, _extra, empty, _duration = build(
                        plasma_project(model_id, dimension, advanced={}), dimension)
                    _model, _extra, materialized, _duration = build(
                        plasma_project(model_id, dimension,
                                       advanced=self.materialized_advanced(model_id)), dimension)
                    self.assertEqual(materialized["nativeAdvancedParameters"]["effective"],
                                     empty["nativeAdvancedParameters"]["effective"])

    def test_empty_advanced_uses_the_parameter_object_overload_with_identical_values(self):
        for dimension in (2, 3):
            for model_id in PLASMA_MODELS:
                with self.subTest(model=model_id, dimension=dimension):
                    plain_model, _extra, plain, _duration = build(plasma_project(model_id, dimension), dimension)
                    advanced_model, _extra, advanced, _duration = build(
                        plasma_project(model_id, dimension, advanced={}), dimension)
                    self.assertEqual(type(plain_model).__name__, type(advanced_model).__name__)
                    self.assertNotIn("nativeAdvancedParameters", plain)
                    metadata = advanced["nativeAdvancedParameters"]
                    self.assertEqual(metadata["parameterClass"], advanced_spec(model_id)["parameterClass"])
                    self.assertEqual(metadata["constructor"], model_spec(model_id)["nativeClass"])
                    self.assertIn("parameter-object", metadata["constructorOverload"])
                    self.assertEqual(metadata["requested"], {})
                    self.assertEqual(metadata["defaults"], metadata["effective"])
                    self.assertEqual(metadata["flatMapping"], advanced_spec(model_id)["flatMapping"])
                    spec = advanced_spec(model_id)
                    plain_params = plain_model.getParameters()
                    for name, block in spec["blocks"].items():
                        native = getattr(plain_params, block["native"])
                        self.assertEqual(metadata["defaults"]["blocks"][name],
                                         {field["key"]: (bool(getattr(native, field["key"]))
                                                         if field["kind"] == "boolean"
                                                         else float(getattr(native, field["key"])))
                                          for field in block["fields"]})
                    for field in spec["scalars"]:
                        value = getattr(plain_params, field["key"])
                        self.assertEqual(metadata["defaults"]["scalars"][field["key"]],
                                         value if field["kind"] == "boolean" else float(value))
                    for row in spec["maps"]:
                        self.assertEqual(metadata["defaults"]["maps"][row["key"]]["default"],
                                         float(getattr(plain_params, row["native"]).getDefault()))

    def test_advanced_overrides_are_applied_on_top_of_the_flat_values(self):
        for model_id in ("sf6o2", "hbro2", "sf6c4f8", "cf4o2"):
            with self.subTest(model=model_id):
                substrate_block = "si" if model_id == "cf4o2" else "substrate"
                advanced = {"ions": {"meanEnergy": 75.0, "sigmaEnergy": 12.0},
                            substrate_block: {"A_ie": 6.0}}
                model, _extra, actual, _duration = build(plasma_project(model_id, 2, advanced), 2)
                metadata = actual["nativeAdvancedParameters"]
                self.assertEqual(metadata["effective"]["blocks"]["ions"]["meanEnergy"], 75.0)
                self.assertEqual(metadata["effective"]["blocks"]["ions"]["sigmaEnergy"], 12.0)
                self.assertEqual(metadata["effective"]["blocks"][substrate_block]["A_ie"], 6.0)
                self.assertEqual(metadata["defaults"]["blocks"]["ions"]["n_l"], 10.0)
                self.assertEqual(type(model).__name__, model_spec(model_id)["nativeClass"])

    def test_model_specific_defaults_survive_the_parameter_object_path(self):
        _model, _extra, actual, _duration = build(
            plasma_project("hbro2", 2, {"ions": {"meanEnergy": 75.0}}), 2)
        defaults = actual["nativeAdvancedParameters"]["defaults"]["blocks"]["substrate"]
        self.assertEqual((defaults["A_ie"], defaults["A_sp"], defaults["Eth_sp"]), (5.0, 0.5, 30.0))

        _model, _extra, actual, _duration = build(
            plasma_project("sf6c4f8", 2, {"ions": {"meanEnergy": 75.0}}), 2)
        metadata = actual["nativeAdvancedParameters"]
        self.assertNotIn("passivationFlux", metadata["defaults"]["scalars"])
        self.assertNotIn("passivation", metadata["defaults"]["blocks"])
        self.assertEqual(metadata["defaults"]["blocks"]["ions"]["exponent"], 300)

        _model, _extra, actual, _duration = build(
            plasma_project("sf6o2", 2, {"ions": {"meanEnergy": 75.0}}), 2)
        metadata = actual["nativeAdvancedParameters"]
        self.assertEqual(metadata["defaults"]["scalars"]["passivationFlux"], 0.2)
        self.assertEqual(metadata["defaults"]["blocks"]["ions"]["exponent"], 100)

    def test_maps_and_boolean_metadata_keep_their_json_types(self):
        model, _extra, actual, _duration = build(
            plasma_project("cf4o2", 2, {
                "gamma_F": {"default": 0.5, "byMaterial": {"Si": 0.2}},
                "fluxIncludeSticking": True,
            }), 2)
        self.assertEqual(type(model).__name__, "CF4O2Etching")
        metadata = actual["nativeAdvancedParameters"]
        map_values = metadata["effective"]["maps"]["gamma_F"]
        self.assertEqual(map_values["default"], 0.5)
        self.assertEqual(map_values["byMaterial"]["Si"], 0.2)
        self.assertEqual(map_values["byMaterial"]["Mask"], 0.7)
        self.assertIs(metadata["effective"]["scalars"]["fluxIncludeSticking"], True)
        self.assertIsInstance(metadata["effective"]["scalars"]["fluxIncludeSticking"], bool)
        defaults = metadata["defaults"]["maps"]["gamma_F"]
        self.assertEqual(defaults["default"], 1.0)
        self.assertEqual(defaults["byMaterial"]["Si"], 0.7)

        _model, _extra, actual, _duration = build(
            plasma_project("sf6o2", 2, {"polymer": {"usePolyCosThetaYield": True}}), 2)
        self.assertIs(actual["nativeAdvancedParameters"]["effective"]["blocks"]["polymer"][
            "usePolyCosThetaYield"], True)
        self.assertIsInstance(actual["nativeAdvancedParameters"]["effective"]["blocks"]["polymer"][
            "usePolyCosThetaYield"], bool)


class AdvancedGoldenRun(unittest.TestCase):
    """Changed advanced values must match a direct native reference."""

    def assert_golden(self, model_id: str, dimension: int, project: dict,
                      top_material: str | None, mutate):
        model, extra, actual, duration = build(project, dimension, top_material)
        workbench_top = run_model(model, duration, extra, dimension, top_material)

        reference, keep_alive = native_reference(model_id, project, dimension, mutate)
        reference_top = run_model(reference, duration, [coverage_parameters()], dimension, top_material)
        self.assertEqual(type(keep_alive).__name__, model_spec(model_id)["nativeClass"])
        self.assertAlmostEqual(workbench_top, reference_top, delta=1e-6)

        plain = copy.deepcopy(project)
        plain["steps"][0]["nativeParameters"].pop("advanced", None)
        plain_model, plain_extra, _plain_actual, plain_duration = build(plain, dimension, top_material)
        plain_top = run_model(plain_model, plain_duration, plain_extra, dimension, top_material)
        self.assertGreater(abs(plain_top - workbench_top), 1e-7, (model_id, plain_top, workbench_top))
        return actual

    def test_sf6o2_2d_and_3d_golden_runs(self):
        advanced = {"ions": {"meanEnergy": 60.0}, "substrate": {"A_ie": 4.0}}

        def mutate(parameters):
            parameters.Ions.meanEnergy = 60.0
            parameters.Substrate.A_ie = 4.0

        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                actual = self.assert_golden(
                    "sf6o2", dimension, plasma_project("sf6o2", dimension, advanced), "SiO2", mutate)
                effective = actual["nativeAdvancedParameters"]["effective"]["blocks"]
                self.assertEqual(effective["ions"]["meanEnergy"], 60.0)
                self.assertEqual(effective["substrate"]["A_ie"], 4.0)

    def test_hbro2_2d_and_3d_golden_runs(self):
        flat = {"ionFlux": 2.0, "etchantFlux": 1.5, "oxygenFlux": 0.35, "meanIonEnergy": 120,
                "sigmaIonEnergy": 15, "ionExponent": 120, "oxySputterYield": 4.0}
        advanced = {"ions": {"meanEnergy": 70.0}, "substrate": {"A_ie": 4.0},
                    "passivation": {"A_ie": 5.0}}

        def mutate(parameters):
            parameters.Ions.meanEnergy = 70.0
            parameters.Substrate.A_ie = 4.0
            parameters.Passivation.A_ie = 5.0

        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                actual = self.assert_golden(
                    "hbro2", dimension,
                    plasma_project("hbro2", dimension, advanced, flat), None, mutate)
                effective = actual["nativeAdvancedParameters"]["effective"]["blocks"]
                self.assertEqual(effective["passivation"]["A_ie"], 5.0)

    def test_sf6c4f8_2d_and_3d_golden_runs_with_maps(self):
        flat = {"ionFlux": 2.0, "etchantFlux": 1.5, "meanEnergy": 120,
                "sigmaEnergy": 15, "ionExponent": 250}
        advanced = {"ions": {"meanEnergy": 70.0}, "substrate": {"A_ie": 8.0},
                    "beta_E": {"byMaterial": {"Si": 0.5}}, "rateFactors": {"default": 0.8}}

        def mutate(parameters):
            parameters.Ions.meanEnergy = 70.0
            parameters.Substrate.A_ie = 8.0
            parameters.beta_E.set(ps.Material.Si, 0.5)
            parameters.rateFactors.setDefault(0.8)

        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                actual = self.assert_golden(
                    "sf6c4f8", dimension,
                    plasma_project("sf6c4f8", dimension, advanced, flat), None, mutate)
                metadata = actual["nativeAdvancedParameters"]["effective"]
                self.assertEqual(metadata["maps"]["beta_E"]["byMaterial"]["Si"], 0.5)
                self.assertEqual(metadata["maps"]["rateFactors"]["default"], 0.8)

    def test_cf4o2_sige_2d_and_3d_golden_runs_with_map_and_bool(self):
        flat = {"ionFlux": 2.0, "etchantFlux": 1.5, "oxygenFlux": 0.35, "polymerFlux": 0.25,
                "meanIonEnergy": 120, "sigmaIonEnergy": 15, "ionExponent": 120,
                "oxySputterYield": 5.0, "polySputterYield": 7.0}
        advanced = {"siGe": {"k_sigma": 200.0, "rho": 4.0},
                    "gamma_F": {"byMaterial": {"SiGe": 0.5}},
                    "fluxIncludeSticking": True}

        def mutate(parameters):
            parameters.SiGe.k_sigma = 200.0
            parameters.SiGe.rho = 4.0
            parameters.fluxIncludeSticking = True
            parameters.gamma_F.set(ps.Material.SiGe, 0.5)

        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                actual = self.assert_golden(
                    "cf4o2", dimension, sige_project(dimension, advanced, flat), "SiGe", mutate)
                metadata = actual["nativeAdvancedParameters"]["effective"]
                self.assertEqual(metadata["blocks"]["siGe"]["k_sigma"], 200.0)
                self.assertEqual(metadata["blocks"]["siGe"]["rho"], 4.0)
                self.assertIs(metadata["scalars"]["fluxIncludeSticking"], True)
                self.assertEqual(metadata["maps"]["gamma_F"]["byMaterial"]["SiGe"], 0.5)


class AdvancedValidationNegatives(unittest.TestCase):
    def assertRejected(self, project: dict, path_fragment: str | None = None):
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project)
        if path_fragment is not None:
            paths = [issue.path for issue in ctx.exception.issues]
            self.assertTrue(any(path_fragment in path for path in paths), paths)
        return ctx.exception

    def test_unknown_top_level_and_nested_keys_are_rejected(self):
        for model_id in PLASMA_MODELS:
            with self.subTest(model=model_id, case="top"):
                self.assertRejected(plasma_project(model_id, advanced={"notANativeBlock": {}}),
                                    "nativeParameters.advanced")
            spec = advanced_spec(model_id)
            block = next(iter(spec["blocks"]))
            with self.subTest(model=model_id, case="nested", block=block):
                self.assertRejected(plasma_project(model_id, advanced={block: {"notAField": 1.0}}),
                                    f"advanced.{block}")

    def test_inert_and_cross_model_blocks_are_rejected(self):
        cases = (
            ("sf6o2", {"si": {"A_ie": 1.0}}),
            ("sf6o2", {"siGe": {"A_ie": 1.0}}),
            ("sf6o2", {"gamma_F": {"default": 0.5}}),
            ("sf6o2", {"fluxIncludeSticking": True}),
            ("sf6o2", {"substrate": {"B_ie": 0.8}}),
            ("cf4o2", {"polymer": {"A_sp": 0.1}}),
            ("cf4o2", {"substrate": {"A_ie": 1.0}}),
            ("cf4o2", {"beta_E": {"default": 0.5}}),
            ("cf4o2", {"passivationFlux": 1.0}),
            ("cf4o2", {"ions": {"thetaRMin": 1.0}}),
            ("cf4o2", {"mask": {"B_sp": 1.0}}),
            ("cf4o2", {"siGe": {"A_ie": 6.5}}),
            ("cf4o2", {"siGe": {"x": 0.4}}),
            ("sf6c4f8", {"passivation": {"A_ie": 3.0}}),
            ("sf6c4f8", {"beta_P": {"default": 1.0}}),
        )
        for model_id, advanced in cases:
            with self.subTest(model=model_id, advanced=advanced):
                self.assertRejected(plasma_project(model_id, advanced=advanced), "advanced")

    def test_sf6c4f8_passivation_flux_is_rejected_by_validation_and_worker(self):
        for value in (0.0, 0.1):
            with self.subTest(value=value):
                project = plasma_project("sf6c4f8", advanced={"passivationFlux": value})
                exception = self.assertRejected(project, "advanced.passivationFlux")
                self.assertTrue(any("no passivation particle" in issue.message for issue in exception.issues),
                                [issue.message for issue in exception.issues])

        parsed, _warnings = validate_project(plasma_project("sf6c4f8"))
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        step = normalized["steps"][0]
        step["nativeParameters"]["advanced"] = {"passivationFlux": 0.1}
        with self.assertRaises(RuntimeError) as ctx:
            build_specialized_model(ps, native_plane(2), normalized, step, native_handle, material_ids())
        self.assertIn("no passivation particle", str(ctx.exception))

    def test_advanced_is_rejected_for_models_without_the_family(self):
        project = make_project("fluorocarbon", 2)
        project["steps"][0]["nativeParameters"]["advanced"] = {}
        self.assertRejected(project)

    def test_wrong_types_non_finite_and_huge_integers_are_rejected(self):
        cases = (
            ("sf6o2", {"ions": []}, "advanced.ions"),
            ("sf6o2", {"ions": {"meanEnergy": "100"}}, "advanced.ions.meanEnergy"),
            ("sf6o2", {"ions": {"meanEnergy": True}}, "advanced.ions.meanEnergy"),
            ("sf6o2", {"ions": {"meanEnergy": float("nan")}}, "advanced.ions.meanEnergy"),
            ("sf6o2", {"ions": {"meanEnergy": float("inf")}}, "advanced.ions.meanEnergy"),
            ("sf6o2", {"ions": {"meanEnergy": 10 ** 400}}, "advanced.ions.meanEnergy"),
            ("sf6o2", {"polymer": {"usePolyCosThetaYield": 1}}, "usePolyCosThetaYield"),
            ("sf6o2", {"passivationFlux": "high"}, "advanced.passivationFlux"),
            ("sf6o2", {"beta_E": []}, "advanced.beta_E"),
            ("sf6o2", {"beta_E": {"unknown": 1.0}}, "advanced.beta_E"),
        )
        for model_id, advanced, fragment in cases:
            with self.subTest(advanced=str(advanced)[:80]):
                self.assertRejected(plasma_project(model_id, advanced=advanced), fragment)

    def test_out_of_range_values_are_rejected(self):
        cases = (
            ("sf6o2", {"ions": {"meanEnergy": -1.0}}, "meanEnergy"),
            ("sf6o2", {"ions": {"minAngle": 4.0}}, "minAngle"),
            ("sf6o2", {"mask": {"rho": 0.0}}, "rho"),
            ("sf6o2", {"substrate": {"rho": -2.0}}, "rho"),
            ("sf6o2", {"passivationFlux": -0.1}, "passivationFlux"),
            ("sf6o2", {"beta_P": {"default": -1.0}}, "beta_P"),
            ("sf6o2", {"beta_P": {"default": 1.1}}, "beta_P"),
            ("sf6c4f8", {"beta_E": {"byMaterial": {"Si": 1.1}}}, "beta_E"),
            ("cf4o2", {"gamma_O": {"default": 1.5}}, "gamma_O"),
            ("cf4o2", {"gamma_C": {"byMaterial": {"Si": -0.1}}}, "gamma_C"),
        )
        for model_id, advanced, fragment in cases:
            with self.subTest(advanced=advanced):
                self.assertRejected(plasma_project(model_id, advanced=advanced), fragment)

    def test_inconsistent_ion_angle_blocks_are_rejected(self):
        cases = (
            ("sf6o2", {"ions": {"thetaRMin": 1.6, "thetaRMax": 1.5}}, "thetaRMin"),
            ("sf6o2", {"ions": {"minAngle": 1.7}}, "minAngle"),
            ("hbro2", {"ions": {"inflectAngle": 1.0}}, "minAngle"),
        )
        for model_id, advanced, fragment in cases:
            with self.subTest(model=model_id, advanced=advanced):
                self.assertRejected(plasma_project(model_id, advanced=advanced), fragment)

    def test_map_material_names_must_exist(self):
        exception = self.assertRejected(
            plasma_project("cf4o2", advanced={"gamma_F": {"byMaterial": {"NotAMaterial": 0.5}}}), "gamma_F")
        self.assertTrue(any("unknown" in issue.message.lower() for issue in exception.issues))

    def test_existing_flat_validation_is_unchanged(self):
        project = plasma_project("sf6o2", advanced={"ions": {"meanEnergy": 75.0}})
        project["steps"][0]["nativeParameters"]["ionFlux"] = -1.0
        self.assertRejected(project, "nativeParameters.ionFlux")


class AdvancedPersistenceAndReplay(unittest.IsolatedAsyncioTestCase):
    ADVANCED = {"ions": {"meanEnergy": 60.0}, "substrate": {"A_ie": 4.0}}

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
        self.fail("plasma advanced run did not finish")

    def test_project_saves_and_reopens_with_advanced_parameters(self):
        storage.initialize()
        project = plasma_project("sf6o2", 2, self.ADVANCED)
        parsed, _warnings = validate_project(project)
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        run_hash = requested_input_hash(parsed)
        record = storage.create_project(normalized)
        reopened = storage.read_json(storage.project_path(record["id"]))["project"]
        self.assertEqual(reopened["steps"][0]["nativeParameters"]["advanced"], self.ADVANCED)
        reparsed, _warnings = validate_project(reopened)
        self.assertEqual(requested_input_hash(reparsed), run_hash)

    async def test_cpu_run_records_metadata_and_replays(self):
        storage.initialize()
        project = plasma_project("sf6o2", 2, self.ADVANCED)
        project["name"] = "plasma advanced CPU replay"
        project["solver"]["enginePreference"] = "cpu"
        project["solver"]["timeoutSeconds"] = 90
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": project})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        await self.wait(run_id)

        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        execution = manifest["actualExecution"][0]
        metadata = execution["actualParameters"]["nativeModel"]["nativeAdvancedParameters"]
        self.assertEqual(metadata["requested"], self.ADVANCED)
        self.assertEqual(metadata["effective"]["blocks"]["ions"]["meanEnergy"], 60.0)
        self.assertEqual(metadata["effective"]["blocks"]["substrate"]["A_ie"], 4.0)

        envelope = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        self.assertEqual(envelope["normalizedProject"]["steps"][0]["nativeParameters"]["advanced"], self.ADVANCED)
        replay = await asgi_request("/api/runs/replay", method="POST", payload=envelope)
        self.assertEqual(replay[0], 202, replay[2])
        replay_id = json_body(replay)["runId"]
        await self.wait(replay_id)
        replay_manifest = storage.read_json(storage.run_path(replay_id) / "manifest.json")
        replay_metadata = replay_manifest["actualExecution"][0]["actualParameters"]["nativeModel"][
            "nativeAdvancedParameters"]
        self.assertEqual(replay_manifest["requestedInputHash"], manifest["requestedInputHash"])
        self.assertEqual(replay_metadata["effective"], metadata["effective"])


if __name__ == "__main__":
    unittest.main()
