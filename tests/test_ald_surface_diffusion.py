"""Native ``SurfaceDiffusionParameters`` on the ALD coverage field.

ViennaPS 4.6.2 binds ``Process.setParameters(SurfaceDiffusionParameters)`` in
2D and 3D, but the object only takes effect when the process model reports
diffusion coefficients. In 4.6.2 the only implementation is
``SingleParticleALDParams.coverageDiffusionCoefficient > 0`` inside the ALP
strategy, where the Laplacian stencil smooths the ``Coverage`` scalar (which
drives the ALD growth rate) before and after each coverage update. It does not
smooth the level-set geometry, and setting it on any other process is inert.

Bounded probes (``/tmp/opencode`` subprocesses, 2D and 3D, plane and trench
structures, with and without a process) established exactly that:

* a zero-coefficient ALD run is bit-identical with and without explicitly
  passing ``SurfaceDiffusionParameters`` (native flag off);
* explicit native defaults are bit-identical to omitting the object;
* changing ``radius``/``sigmaNormal``/``kNeighbors``/``stabilityFactor``/
  ``symmetrizeWeights`` changes the deposited surface deterministically
  (repeated runs hash equal) while the material map stays unchanged;
* the same parameters on an isotropic/single-particle process do nothing.

These tests pin the contract: only ``specialized:ald`` accepts the object,
``coverageDiffusionCoefficient`` must be positive, the worker calls the real
native setter and records the effective values, and omitting the object leaves
the historical request hashes byte-identical.
"""
from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import viennaps as ps  # noqa: E402

from backend import storage  # noqa: E402
from backend.hashing import requested_input_hash  # noqa: E402
from backend.main import app  # noqa: E402
from backend.models import SurfaceDiffusionV7  # noqa: E402
from backend.validation import ProjectInputError, validate_project  # noqa: E402
from stage8_direct_reference import make_domain, make_model  # noqa: E402
from stage8_project_factory import make_project  # noqa: E402
from backend.engine import ald_surface_diffusion_supported
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from worker.simulate import MATERIAL_IDS, configure_advection, configure_rays, material_handle  # noqa: E402
from worker.specialized import build_specialized_model  # noqa: E402

# Request hashes on base eb5a40c, before ``surfaceDiffusion`` existed.
BASE_REQUEST_HASHES = {
    2: "3c7c811abfc8ec1d2c715bb56be7126981d746f574239e666bcd58326f27db1a",
    3: "7d5ca40fca84a9f1afcc627ffd7323313a8e2f8787e200de249e22bcbf523953",
}

NATIVE_DEFAULTS = {
    "kNeighbors": 16,
    "normalCutoff": 0.25,
    "normalizeByLocalScale": True,
    "radius": 0.0,
    "sigmaNormal": 0.35,
    "stabilityFactor": 1.0,
    "symmetrizeWeights": True,
}


def ald_project(dimension: int = 2, *, coefficient: float = 5.0,
                surface_diffusion: dict | None = None) -> dict:
    """ALD on a tall nitride trench so the coverage field has a gradient."""
    project = make_project("ald", dimension)
    project["geometry"] = {
        "kind": "stack",
        "substrateMaterial": "Si",
        "baseHeight": 0.0,
        "layers": [
            {"id": "oxide", "name": "Oxide", "material": "SiO2", "thickness": 0.4, "role": "film"},
            {"id": "nitride", "name": "Nitride", "material": "Si3N4", "thickness": 1.2, "role": "mask"},
        ],
        "pattern": {"kind": "trench", "width": 0.6, "taperAngle": 0.0},
    }
    project["domain"] = {"gridDelta": 0.2, "xExtent": 4.0,
                         **({"xBoundary": "reflective"} if dimension == 2
                            else {"yExtent": 4.0, "boundaryXY": "reflective"})}
    step = project["steps"][0]
    step["nativeParameters"].update({
        "coverageDiffusionCoefficient": coefficient,
        "stickingProbability": 1.0,
        "evaporationFlux": 0.0,
        "growthPerCycle": 0.05,
        "numCycles": 2,
    })
    project["solver"]["raysPerPoint"] = 64
    project["solver"]["timeoutSeconds"] = 300
    if surface_diffusion is not None:
        step["surfaceDiffusion"] = copy.deepcopy(surface_diffusion)
    return project


def normalized(project: dict) -> dict:
    return validate_project(project)[0].model_dump(mode="json", exclude_none=True)


def _heights(domain, dimension: int) -> list[float]:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    vertical = 1 if dimension == 2 else 2
    return [float(point[vertical]) for point in mesh.getNodes()]


def _materials(domain) -> list[int]:
    mapping = domain.getMaterialMap()
    return [int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size())]


def _apply(process, project: dict, extra: list) -> None:
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection, _actual = configure_advection(ps, project)
    process.setParameters(advection)
    rays, _actual = configure_rays(ps, project, 271828)
    process.setParameters(rays)
    for item in extra:
        process.setParameters(item)
    process.apply()


def run_worker(project: dict) -> dict:
    """Run the exact worker construction path and return shape plus record."""
    domain = make_domain(project)
    domain.duplicateTopLevelSet(ps.Material.Al2O3)
    step = project["steps"][0]
    model, extra, resolved, duration = build_specialized_model(
        ps, domain, project, step,
        lambda name: material_handle(ps, name), MATERIAL_IDS)
    process = ps.Process(domain, model, duration)
    _apply(process, project, extra)
    return {
        "record": resolved,
        "heights": _heights(domain, project["dimension"]),
        "materials": _materials(domain),
        "processParameters": [type(item).__name__ for item in extra],
    }


def run_reference(project: dict, mutate) -> list[float]:
    """Independent native reference that mutates the native parameter object."""
    domain = make_domain(project)
    domain.duplicateTopLevelSet(ps.Material.Al2O3)
    step = project["steps"][0]
    model, extra = make_model(step, domain, project)
    mutate(extra[-1])
    process = ps.Process(domain, model, float(step["duration"]))
    _apply(process, project, extra)
    return _heights(domain, project["dimension"])


class SurfaceDiffusionSchema(unittest.TestCase):
    def test_schema_defaults_match_the_installed_binding(self):
        from pydantic import ValidationError

        schema = SurfaceDiffusionV7()
        native = ps.SurfaceDiffusionParameters()
        for key, value in NATIVE_DEFAULTS.items():
            self.assertEqual(getattr(schema, key), value, key)
            self.assertEqual(getattr(native, key), value, key)
        with self.assertRaises(ValidationError):
            SurfaceDiffusionV7(sigmaNormal=0.0)
        with self.assertRaises(ValidationError):
            SurfaceDiffusionV7(stabilityFactor=1.5)
        with self.assertRaises(ValidationError):
            SurfaceDiffusionV7(radius=-0.1)

    def test_omitting_the_object_keeps_the_base_request_hashes(self):
        for dimension, expected in BASE_REQUEST_HASHES.items():
            with self.subTest(dimension=dimension):
                project = make_project("ald", dimension)
                self.assertEqual(requested_input_hash(validate_project(project)[0]), expected)

    def test_adding_the_object_changes_the_request_hash(self):
        plain = ald_project(2)
        with_diffusion = ald_project(2, surface_diffusion={"radius": 0.1})
        # Hashes are runtime-independent; validate against the 4.6.2 acceptance
        # path so this test also runs on the 4.7.0 evaluation runtime.
        with patch("backend.validation.engine_version", return_value="4.6.2"):
            self.assertNotEqual(requested_input_hash(validate_project(plain)[0]),
                                requested_input_hash(validate_project(with_diffusion)[0]))

    def test_placement_is_ald_only_and_enabled_requires_positive_coefficient(self):
        good = ald_project(2, surface_diffusion={"radius": 0.1})
        with patch("backend.validation.engine_version", return_value="4.6.2"):
            validate_project(good)

        zero_coefficient = ald_project(2, coefficient=0.0, surface_diffusion={"radius": 0.1})
        with self.assertRaises(ProjectInputError) as context:
            validate_project(zero_coefficient)
        self.assertIn("coverageDiffusionCoefficient > 0", str(context.exception))

        other = make_project("teosDeposition", 2)
        other["steps"][0]["surfaceDiffusion"] = {"radius": 0.1}
        with self.assertRaises(ProjectInputError) as context:
            validate_project(other)
        self.assertIn("specialized:ald", str(context.exception))

        # A disabled token is still ALD-only: placement is a schema fact, not
        # something the enabled flag can bypass.
        other_disabled = make_project("teosDeposition", 2)
        other_disabled["steps"][0]["surfaceDiffusion"] = {"enabled": False}
        with self.assertRaises(ProjectInputError) as context:
            validate_project(other_disabled)
        self.assertIn("specialized:ald", str(context.exception))

        disabled = ald_project(2, coefficient=0.0, surface_diffusion={"enabled": False})
        parsed, _warnings = validate_project(disabled)
        self.assertIsNotNone(parsed.steps[0].surfaceDiffusion)
        self.assertFalse(parsed.steps[0].surfaceDiffusion.enabled)

        disabled_override = ald_project(2, surface_diffusion={"enabled": False, "radius": 0.1})
        with self.assertRaises(ProjectInputError) as context:
            validate_project(disabled_override)
        self.assertIn("omit the object", str(context.exception))

    def test_v470_runtime_rejects_enabled_block_before_the_worker(self):
        project = ald_project(2, surface_diffusion={"radius": 0.1})
        with patch("backend.validation.engine_version", return_value="4.7.0"):
            with self.assertRaises(ProjectInputError) as context:
                validate_project(project)
        message = str(context.exception)
        self.assertIn("steps[0].surfaceDiffusion", message)
        self.assertIn("4.7.0", message)
        self.assertIn("SurfaceDiffusionParameters", message)
        self.assertIn("hang", message)

    def test_v462_runtime_accepts_the_enabled_block(self):
        project = ald_project(2, surface_diffusion={"radius": 0.1})
        with patch("backend.validation.engine_version", return_value="4.6.2"):
            parsed, _warnings = validate_project(project)
        self.assertTrue(parsed.steps[0].surfaceDiffusion.enabled)

    def test_v470_runtime_accepts_an_explicitly_disabled_token(self):
        project = ald_project(2, coefficient=0.0,
                              surface_diffusion={"enabled": False})
        with patch("backend.validation.engine_version", return_value="4.7.0"):
            parsed, _warnings = validate_project(project)
        self.assertFalse(parsed.steps[0].surfaceDiffusion.enabled)

    def test_unknown_keys_and_bad_values_are_rejected(self):
        for mutation in (
            {"radius": 11.0},
            {"radius": 1.7},  # schema-legal, but > 8 * gridDelta (0.2)
            {"kNeighbors": 0},
            {"kNeighbors": 65},
            {"normalCutoff": 1.5},
            {"stabilityFactor": 0.0},
            {"sigmaNormal": -1.0},
            {"extra": 1.0},
        ):
            with self.subTest(mutation=mutation):
                project = ald_project(2, surface_diffusion=mutation)
                with self.assertRaises(ProjectInputError):
                    validate_project(project)
        non_finite = ald_project(2, surface_diffusion={"radius": float("nan")})
        with self.assertRaises(ProjectInputError):
            validate_project(non_finite)


class SurfaceDiffusionNative(unittest.TestCase):
    """The worker path must equal an independent native reference exactly."""

    PARAMS = {"radius": 0.1, "sigmaNormal": 0.2, "kNeighbors": 8,
              "stabilityFactor": 0.5, "symmetrizeWeights": True,
              "normalizeByLocalScale": True, "normalCutoff": 0.25}

    def mutate(self, parameters) -> None:
        parameters.radius = self.PARAMS["radius"]
        parameters.sigmaNormal = self.PARAMS["sigmaNormal"]
        parameters.kNeighbors = self.PARAMS["kNeighbors"]
        parameters.stabilityFactor = self.PARAMS["stabilityFactor"]
        parameters.symmetrizeWeights = self.PARAMS["symmetrizeWeights"]
        parameters.normalizeByLocalScale = self.PARAMS["normalizeByLocalScale"]
        parameters.normalCutoff = self.PARAMS["normalCutoff"]

    @unittest.skipUnless(ald_surface_diffusion_supported(),
                         "4.7.0 has a confirmed native ALD surface-diffusion hang")
    def test_golden_2d_and_3d_equal_the_native_reference_and_are_deterministic(self):
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                project = normalized(ald_project(dimension, surface_diffusion=self.PARAMS))
                first = run_worker(project)
                reference = run_reference(project, self.mutate)
                self.assertEqual(len(first["heights"]), len(reference))
                self.assertLessEqual(max(abs(a - b) for a, b in zip(first["heights"], reference)),
                                     1e-6)
                self.assertIn("SurfaceDiffusionParameters", first["processParameters"])

                record = first["record"]["surfaceDiffusion"]
                self.assertEqual(record["nativeClass"], "SurfaceDiffusionParameters")
                self.assertEqual(record["effective"], self.PARAMS)
                self.assertEqual(record["nativeInsertionPoint"],
                                 "Process.setParameters(SurfaceDiffusionParameters)")
                self.assertEqual(record["nativeMetaData"]["Radius"], [self.PARAMS["radius"]])

                repeat = run_worker(project)
                self.assertEqual(first["heights"], repeat["heights"])
                self.assertEqual(first["materials"], repeat["materials"])

    @unittest.skipUnless(ald_surface_diffusion_supported(),
                         "4.7.0 has a confirmed native ALD surface-diffusion hang")
    def test_the_knob_changes_the_surface_and_preserves_materials(self):
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                plain = run_worker(normalized(ald_project(dimension)))
                smoothed = run_worker(normalized(
                    ald_project(dimension, surface_diffusion=self.PARAMS)))
                self.assertNotIn("surfaceDiffusion", plain["record"])
                self.assertGreater(
                    max(abs(a - b) for a, b in zip(plain["heights"], smoothed["heights"])),
                    1e-6)
                # Coverage diffusion redistributes coverage; it must not add or
                # remove level sets or change material identities.
                self.assertEqual(plain["materials"], smoothed["materials"])

    def test_explicit_native_defaults_are_a_behaviour_noop(self):
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                omitted = run_worker(normalized(ald_project(dimension)))
                # Native default parameters do not trigger the 4.7.0 hang, so
                # exercise the worker path directly (bypassing the request
                # gate) to prove the explicit object is still a no-op there.
                with patch("backend.validation.engine_version", return_value="4.6.2"), \
                        patch("backend.engine.engine_version", return_value="4.6.2"):
                    explicit = run_worker(normalized(
                        ald_project(dimension, surface_diffusion=dict(NATIVE_DEFAULTS))))
                self.assertEqual(omitted["heights"], explicit["heights"])
                self.assertEqual(omitted["materials"], explicit["materials"])
                self.assertIn("surfaceDiffusion", explicit["record"])

    def test_disabled_token_on_zero_coefficient_ald_is_an_inert_noop(self):
        without = run_worker(normalized(ald_project(2, coefficient=0.0)))
        disabled = run_worker(normalized(ald_project(
            2, coefficient=0.0, surface_diffusion={"enabled": False})))
        self.assertEqual(without["heights"], disabled["heights"])
        self.assertEqual(without["materials"], disabled["materials"])
        self.assertNotIn("surfaceDiffusion", disabled["record"])
        self.assertNotIn("SurfaceDiffusionParameters", disabled["processParameters"])

    def test_worker_rejects_an_inert_configuration(self):
        project = normalized(ald_project(2, coefficient=0.0,
                                         surface_diffusion={"enabled": False}))
        step = project["steps"][0]
        # Bypass the schema check to prove the worker itself also refuses a
        # natively inert setter call.
        step["surfaceDiffusion"] = dict(NATIVE_DEFAULTS)
        domain = make_domain(project)
        with self.assertRaises(RuntimeError):
            build_specialized_model(ps, domain, project, step,
                                    lambda name: material_handle(ps, name), MATERIAL_IDS)

    def test_worker_gate_rejects_the_hanging_config_on_v470(self):
        # Bypass API validation (as a hand-edited worker input would) and prove
        # the builder itself fails before constructing the native object.
        with patch("backend.validation.engine_version", return_value="4.6.2"):
            project = normalized(ald_project(2, surface_diffusion=self.PARAMS))
        step = project["steps"][0]
        domain = make_domain(project)
        with patch("backend.engine.engine_version", return_value="4.7.0"):
            with self.assertRaises(RuntimeError) as context:
                build_specialized_model(ps, domain, project, step,
                                        lambda name: material_handle(ps, name), MATERIAL_IDS)
        message = str(context.exception)
        self.assertIn("4.7.0", message)
        self.assertIn("SurfaceDiffusionParameters", message)
        self.assertIn("never return", message)

    def test_worker_accepts_the_config_when_runtime_is_462(self):
        with patch("backend.validation.engine_version", return_value="4.6.2"):
            project = normalized(ald_project(2, surface_diffusion=self.PARAMS))
        step = project["steps"][0]
        domain = make_domain(project)
        with patch("backend.engine.engine_version", return_value="4.6.2"):
            _model, extra, _resolved, _duration = build_specialized_model(
                ps, domain, project, step,
                lambda name: material_handle(ps, name), MATERIAL_IDS)
        self.assertIn("SurfaceDiffusionParameters",
                      [type(item).__name__ for item in extra])

    def test_worker_rejects_a_domain_wide_radius(self):
        with patch("backend.validation.engine_version", return_value="4.6.2"):
            project = normalized(ald_project(2, surface_diffusion={"radius": 0.1}))
        project["steps"][0]["surfaceDiffusion"]["radius"] = 1.7
        domain = make_domain(project)
        with patch("backend.engine.engine_version", return_value="4.6.2"):
            with self.assertRaises(RuntimeError) as context:
                build_specialized_model(ps, domain, project, project["steps"][0],
                                        lambda name: material_handle(ps, name), MATERIAL_IDS)
        self.assertIn("8 grid intervals", str(context.exception))


class SurfaceDiffusionPersistenceAndReplay(unittest.IsolatedAsyncioTestCase):
    PARAMS = {"radius": 0.1, "sigmaNormal": 0.2, "stabilityFactor": 0.5}

    async def asyncSetUp(self):
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)

    async def wait(self, run_id: str) -> dict:
        for _ in range(2400):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}:
                self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail("ALD surface-diffusion run did not finish")

    def test_project_saves_and_reopens_with_surface_diffusion(self):
        storage.initialize()
        project = ald_project(2, surface_diffusion=self.PARAMS)
        if not ald_surface_diffusion_supported():
            with self.assertRaises(ProjectInputError) as raised:
                validate_project(project)
            self.assertIn("surfaceDiffusion", str(raised.exception))
            return
        parsed, _warnings = validate_project(project)
        normalized_project = parsed.model_dump(mode="json", exclude_none=True)
        run_hash = requested_input_hash(parsed)
        record = storage.create_project(normalized_project)
        reopened = storage.read_json(storage.project_path(record["id"]))["project"]
        self.assertEqual(reopened["steps"][0]["surfaceDiffusion"],
                         {"enabled": True, **NATIVE_DEFAULTS, **self.PARAMS})
        reparsed, _warnings = validate_project(reopened)
        self.assertEqual(requested_input_hash(reparsed), run_hash)

    async def test_cpu_run_records_effective_values_and_replays(self):
        storage.initialize()
        project = ald_project(2, surface_diffusion=self.PARAMS)
        project["name"] = "ALD surface diffusion replay"
        project["solver"]["enginePreference"] = "cpu"
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": project})
        if not ald_surface_diffusion_supported():
            self.assertEqual(response[0], 422, response[2])
            detail = json_body(response)["detail"]
            issues = detail if isinstance(detail, list) else detail.get("issues", [detail])
            self.assertTrue(any("surfaceDiffusion" in str(item) and "hang" in str(item)
                                for item in issues), detail)
            return
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        await self.wait(run_id)

        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        record = manifest["actualExecution"][0]["actualParameters"]["nativeModel"]["surfaceDiffusion"]
        expected = {"enabled": True, **NATIVE_DEFAULTS, **self.PARAMS}
        self.assertEqual(record["nativeClass"], "SurfaceDiffusionParameters")
        self.assertEqual(record["requested"], expected)
        self.assertEqual(record["effective"], {**NATIVE_DEFAULTS, **self.PARAMS})
        self.assertEqual(record["coverageDiffusionCoefficient"], 5.0)

        envelope = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        self.assertEqual(envelope["normalizedProject"]["steps"][0]["surfaceDiffusion"], expected)
        replay = await asgi_request("/api/runs/replay", method="POST", payload=envelope)
        self.assertEqual(replay[0], 202, replay[2])
        replay_id = json_body(replay)["runId"]
        await self.wait(replay_id)
        replay_manifest = storage.read_json(storage.run_path(replay_id) / "manifest.json")
        self.assertEqual(replay_manifest["requestedInputHash"], manifest["requestedInputHash"])
        replay_record = replay_manifest["actualExecution"][0]["actualParameters"]["nativeModel"][
            "surfaceDiffusion"]
        self.assertEqual(replay_record["effective"], record["effective"])


if __name__ == "__main__":
    unittest.main()
