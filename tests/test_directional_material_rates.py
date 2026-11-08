"""Directional material-rate-table contract tests.

The native ``DirectionalProcess`` binding exposes a rate-map overload

    DirectionalProcess(direction, {material: (directional, isotropic)},
                       defaultDirectionalRate, defaultIsotropicRate)

which has no mask-material or visibility argument.  These tests pin the new
schema fields, the validation rules, the default-rate protection and a
direct native reference comparison in 2D and 3D.  Projects that omit the new
fields must keep their legacy scalar/``rateSets`` hash and normalization.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import unittest
from pathlib import Path

from backend import snapshots as snapshot_store
from backend import storage
from backend.hashing import requested_input_hash
from backend.main import app
from backend.materials import referenced_material_names
from backend.rates import has_motion
from backend.validation import ProjectInputError, validate_project
from tests.test_api_integration import asgi_request, json_body
from tests.test_directional import (project as directional_project,
                                   directional_step, rate_set_step)


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = storage.VAR / "test-directional-material-rates"

# Captured from the pre-feature commit; optional fields must not move them.
FROZEN_HASHES = {
    (2, "scalar"): "649460277660d2d0d2553f3d3c58432b39e14f084e9be0a16403adb891dcfecb",
    (2, "rateSets"): "3e6c8b312c85f9c366d4321694f9d3f96f6cfff8d8863f8d2d69c5c80e2c2c33",
    (3, "scalar"): "be995601e436454b8ac06d6fefb3abbf35f6b98f73627d1c9a934181a05393ae",
    (3, "rateSets"): "3a167c9cb3319faf727d20b3aacb61687305e4c2ebf6b1a3d5f6f36ef6e750f1",
}

# Independent native reference.  It is passed to the bounded child through
# ``python -c`` so the test owns no extra repository file.
REFERENCE_SCRIPT = r'''
import json
import math
import sys
from pathlib import Path

import viennaps as ps


def material(name):
    registry = ps.MaterialRegistry.instance()
    if registry.findMaterial(name) is None:
        raise RuntimeError(f"material {name!r} is not registered")
    return registry.getMaterial(name)


def build_domain(project):
    ps.setDimension(project["dimension"])
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    delta = float(project["domain"]["gridDelta"])
    boundary_name = project["domain"].get("boundaryXY", project["domain"].get("xBoundary"))
    boundary = (ps.BoundaryType.REFLECTIVE_BOUNDARY if boundary_name == "reflective"
                else ps.BoundaryType.PERIODIC_BOUNDARY)
    if project["dimension"] == 2:
        domain = ps.Domain(delta, float(project["domain"]["xExtent"]), boundary=boundary)
    else:
        domain = ps.Domain(delta, float(project["domain"]["xExtent"]),
                           float(project["domain"]["yExtent"]), boundary)
    geometry = project["geometry"]
    if geometry["kind"] == "plane":
        ps.MakePlane(domain, height=float(geometry["height"]),
                     material=material(geometry["substrateMaterial"]),
                     addToExisting=False).apply()
    elif geometry["kind"] == "trench":
        ps.MakeTrench(domain=domain, trenchWidth=float(geometry["width"]),
                      trenchDepth=float(geometry["depth"]),
                      trenchTaperAngle=float(geometry["taperAngle"]),
                      maskHeight=float(geometry["maskHeight"]),
                      maskTaperAngle=float(geometry["maskTaperAngle"]),
                      halfTrench=False,
                      material=material(geometry["substrateMaterial"]),
                      maskMaterial=material(geometry.get("maskMaterial", "Mask"))).apply()
    else:
        raise ValueError("reference supports plane and trench inputs")
    return domain


def snapshot(domain, step_id):
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False,
                                 minNodeDistanceFactor=0.01)
    dim = 2 if not mesh.getTriangles() else 3
    points = [[float(value) for value in point] for point in mesh.getNodes()]
    cells = [[int(value) for value in cell]
             for cell in (mesh.getLines() if dim == 2 else mesh.getTriangles())]
    data = mesh.getCellData()
    material_ids = []
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = [int(value) for value in data.getScalarData(index)]
    mapping = domain.getMaterialMap()
    material_map = [int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size())]
    return {"stepId": step_id, "points": points, "cells": cells,
            "materialIds": material_ids, "materialMap": material_map}


def run_step(domain, project, step):
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(material(step["depositMaterial"]))
    etch = step["operation"] == "etch"
    directional_sign = 1.0 if etch else -1.0
    isotropic_sign = -1.0 if etch else 1.0
    direction = [float(value) for value in step["direction"]]
    norm = math.sqrt(sum(value * value for value in direction))
    if norm <= 1e-12:
        raise RuntimeError("invalid zero reference direction")
    direction = [value / norm for value in direction]
    table = {
        material(name): (directional_sign * float(row["directional"]),
                         isotropic_sign * float(row.get("isotropic", 0.0)))
        for name, row in step["materialRates"].items()
    }
    default = step.get("defaultMaterialRate") or {}
    model = ps.DirectionalProcess(
        direction, table,
        directional_sign * float(default.get("directional", 0.0)),
        isotropic_sign * float(default.get("isotropic", 0.0)))
    process = ps.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
    process.setParameters(advection)
    process.apply()


def main():
    source = Path(sys.argv[1])
    destination = Path(sys.argv[2])
    project = json.loads(source.read_text(encoding="utf-8"))
    domain = build_domain(project)
    output = {"initial": snapshot(domain, "initial")}
    for step in project["steps"]:
        if step["enabled"]:
            run_step(domain, project, step)
        output[step["id"]] = snapshot(domain, step["id"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"stepIds": list(output)}))


if __name__ == "__main__":
    main()
'''


def directional_material_rate(directional: float = 0.0, isotropic: float = 0.0) -> dict:
    return {"directional": directional, "isotropic": isotropic}


def material_step(operation: str = "etch", *, direction=None, rates=None,
                  default=None, visibility=None, deposit_material=None,
                  duration: float = 0.3, step_id: str = "step_dir_material",
                  **extra) -> dict:
    step = {
        "id": step_id,
        "name": "材料速率表定向",
        "enabled": True,
        "operation": operation,
        "model": "directional",
        "direction": direction if direction is not None else [0.0, -1.0, 0.0],
        "materialRates": rates if rates is not None else {"Si": directional_material_rate(0.3, 0.05)},
        "duration": duration,
    }
    if default is not None:
        step["defaultMaterialRate"] = default
    if visibility is not None:
        step["calculateVisibility"] = visibility
    if operation == "deposit":
        step["depositMaterial"] = deposit_material or "SiO2"
    step.update(extra)
    return step


def walk_items(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from walk_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_items(child)


def walk_numbers(value):
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield float(value)
    elif isinstance(value, dict):
        for child in value.values():
            yield from walk_numbers(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_numbers(child)


def walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_strings(child)


def vertical_axis(dimension: int) -> int:
    return 1 if dimension == 2 else 2


def material_vertices(snapshot: dict, material_id: int) -> set[int]:
    cells = snapshot.get("triangles") or snapshot.get("lines") or []
    ids = snapshot["cellData"]["materialIds"]
    return {vertex for cell, current in zip(cells, ids)
            if int(current) == int(material_id) for vertex in cell}


def material_top(snapshot: dict, material_id: int, axis: int) -> float | None:
    vertices = material_vertices(snapshot, material_id)
    if not vertices:
        return None
    return max(float(snapshot["points"][index][axis]) for index in vertices)


def exposed_floor(snapshot: dict, material_id: int, axis: int, is_3d: bool) -> float | None:
    vertices = material_vertices(snapshot, material_id)
    if not vertices:
        return None
    selected = []
    for index in vertices:
        point = snapshot["points"][index]
        if abs(float(point[0])) > 0.4:
            continue
        if is_3d and abs(float(point[1])) > 0.4:
            continue
        selected.append(float(point[axis]))
    return max(selected) if selected else None


class DirectionalMaterialRateSchemaTests(unittest.TestCase):
    def test_material_mode_is_accepted_and_normalized(self):
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                value = directional_project(dimension)
                direction = [0.0, -1.0, 0.0] if dimension == 2 else [0.0, 0.0, -1.0]
                value["steps"] = [material_step(
                    direction=direction,
                    rates={"Si": directional_material_rate(0.25, 0.05),
                           "Mask": directional_material_rate(0.0, 0.0)})]
                parsed, _warnings = validate_project(value, kind="simulate")
                normalized = parsed.model_dump(mode="json", exclude_none=True)
                step = normalized["steps"][0]
                self.assertEqual(step["materialRates"]["Si"],
                                 {"directional": 0.25, "isotropic": 0.05})
                self.assertNotIn("directionalRateMagnitude", step)
                self.assertNotIn("isotropicRateMagnitude", step)
                self.assertNotIn("rateSets", step)
                self.assertNotIn("defaultMaterialRate", step)
                self.assertEqual(step["direction"], direction)
                self.assertTrue(has_motion(parsed.steps[0]))

        value = directional_project(3)
        value["steps"] = [material_step(
            operation="deposit", direction=[0.0, 0.0, -1.0], deposit_material="W",
            rates={"Si": directional_material_rate(0.2, 0.0)},
            default=directional_material_rate(0.05, 0.01))]
        parsed, _warnings = validate_project(value, kind="simulate")
        normalized = parsed.model_dump(mode="json", exclude_none=True)["steps"][0]
        self.assertEqual(normalized["defaultMaterialRate"],
                         {"directional": 0.05, "isotropic": 0.01})
        self.assertTrue(has_motion(parsed.steps[0]))

    def test_legacy_scalar_and_rate_set_hashes_are_frozen(self):
        for dimension in (2, 3):
            direction = [0.0, -1.0, 0.0] if dimension == 2 else [0.0, 0.0, -1.0]
            cases = {
                "scalar": [directional_step(direction=direction)],
                "rateSets": [rate_set_step()],
            }
            for label, steps in cases.items():
                with self.subTest(dimension=dimension, mode=label):
                    value = directional_project(dimension)
                    value["steps"] = steps
                    parsed, _warnings = validate_project(value)
                    self.assertEqual(requested_input_hash(parsed),
                                     FROZEN_HASHES[(dimension, label)])
                    step = parsed.model_dump(mode="json", exclude_none=True)["steps"][0]
                    self.assertNotIn("materialRates", step)
                    self.assertNotIn("defaultMaterialRate", step)

    def test_mutually_exclusive_and_malformed_modes_are_rejected(self):
        def reject(mutate, label):
            value = directional_project(2)
            value["steps"] = [material_step()]
            mutate(value["steps"][0])
            with self.subTest(label=label):
                with self.assertRaises(ProjectInputError):
                    validate_project(value, kind="simulate")

        reject(lambda step: step.update(directionalRateMagnitude=0.1), "scalar directional rate")
        reject(lambda step: step.update(isotropicRateMagnitude=0.1), "scalar isotropic rate")
        reject(lambda step: step.update(rateSets=[{
            "direction": [0.0, 1.0, 0.0], "directionalVelocity": 0.1}]),
            "rateSets together with materialRates")
        reject(lambda step: step.update(calculateVisibility=False), "visibility control")
        reject(lambda step: step.pop("direction"), "missing direction")
        reject(lambda step: step.update(direction=[0.0, 0.0, 0.0]), "zero direction")
        reject(lambda step: step.update(direction=[0.0, -1.0, 0.5]), "2D third component")
        reject(lambda step: step.update(materialRates={"NotAMaterial":
                                                       directional_material_rate(0.1, 0.0)}),
               "unknown material")
        reject(lambda step: step.update(materialRates={"Si3N4":
                                                       directional_material_rate(0.1, 0.0)}),
               "catalog material unavailable at this step")
        reject(lambda step: step.update(materialRates={"Si":
                                                       directional_material_rate(-0.1, 0.0)}),
               "negative rate")
        reject(lambda step: step.update(materialRates={"Si":
                                                       directional_material_rate(2.5, 0.0)}),
               "rate above bound")
        reject(lambda step: step.update(materialRates={"Si":
                                                       directional_material_rate(0.0, 0.0)}),
               "zero effective motion")
        reject(lambda step: step.update(materialRates={"Si":
                                                       directional_material_rate(0.0, 0.0)},
                                        defaultMaterialRate=directional_material_rate(0.0, 0.0)),
               "explicit zero default")

        # rateSets stays mutually exclusive with the new default table too.
        value = directional_project(2)
        value["steps"] = [rate_set_step()]
        value["steps"][0]["defaultMaterialRate"] = directional_material_rate(0.0, 0.0)
        with self.assertRaises(ProjectInputError):
            validate_project(value, kind="simulate")

    def test_material_mode_direction_is_normalized_and_hashed(self):
        value = directional_project(3)
        value["steps"] = [material_step(
            direction=[0.0, 0.0, -1.0],
            rates={"Si": directional_material_rate(0.2, 0.1)},
            default=directional_material_rate(0.0, 0.0))]
        parsed, _ = validate_project(value)
        baseline = requested_input_hash(parsed)
        scaled = copy.deepcopy(value)
        scaled["steps"][0]["direction"] = [0.0, 0.0, -0.5]
        self.assertEqual(baseline, requested_input_hash(validate_project(scaled)[0]))
        changed = copy.deepcopy(value)
        changed["steps"][0]["direction"] = [1.0, 0.0, 0.0]
        self.assertNotEqual(baseline, requested_input_hash(validate_project(changed)[0]))

    def test_material_table_names_are_tracked_as_references(self):
        names = referenced_material_names({"geometry": {"kind": "plane"}, "steps": [
            {"model": "directional",
             "materialRates": {"CustomHardmask": {"directional": 0.1, "isotropic": 0.0}}}]})
        self.assertEqual(names, {"CustomHardmask"})


class DirectionalMaterialRateWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)

    async def wait(self, run_id: str) -> dict:
        for _ in range(1200):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
                self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail("bounded native worker exceeded the acceptance timeout")

    async def execute(self, value: dict) -> tuple[str, dict]:
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": value})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        return run_id, await self.wait(run_id)

    def snapshot(self, run_id: str, step_id: str) -> dict:
        path = snapshot_store.ensure_materialized(storage.run_path(run_id), step_id)
        return storage.read_json(path)

    async def reference(self, value: dict, label: str) -> dict:
        source = REFERENCE_DIR / f"{label}-input.json"
        destination = REFERENCE_DIR / f"{label}-direct-reference.json"
        guard = REFERENCE_DIR / f"{label}-guard.json"
        log = REFERENCE_DIR / f"{label}-reference.log"
        source.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        command = [
            str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/bounded_child.py"),
            "--cwd", str(ROOT), "--record", str(guard), "--log-path", str(log),
            "--timeout-seconds", "60", "--max-rss-mib", "2048",
            "--max-log-bytes", "2097152", "--",
            str(ROOT / ".venv/bin/python"), "-c", REFERENCE_SCRIPT,
            str(source), str(destination),
        ]
        completed = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL)
        self.assertEqual(await completed.wait(), 0,
                         log.read_text(encoding="utf-8", errors="replace"))
        return json.loads(destination.read_text(encoding="utf-8"))

    async def compare(self, value: dict, label: str) -> tuple[str, dict, dict]:
        run_id, info = await self.execute(value)
        direct = await self.reference(value, label)
        step_id = value["steps"][-1]["id"]
        key = "lines" if value["dimension"] == 2 else "triangles"
        actual = self.snapshot(run_id, step_id)
        expected = direct[step_id]
        self.assertEqual(actual[key], expected["cells"])
        self.assertEqual(actual["cellData"]["materialIds"], expected["materialIds"])
        self.assertEqual(actual["materialIdsByLevelSetIndex"], expected["materialMap"])
        self.assertEqual(len(actual["points"]), len(expected["points"]))
        error = max((math.dist(left, right) for left, right in
                     zip(actual["points"], expected["points"])), default=0.0)
        self.assertLessEqual(error, 1e-10, f"{label} coordinate error {error}")
        return run_id, info, expected

    def assert_material_metadata(self, parameters: dict, *, operation: str,
                                 directional: float, isotropic: float,
                                 material_name: str = "Si",
                                 material_id: int = 10) -> None:
        text = json.dumps(parameters, ensure_ascii=False)
        lower = text.lower()
        self.assertIn("directionalprocess", lower)
        self.assertIn("material", lower)
        self.assertIn("default", lower)
        self.assertTrue(any("material" in value.lower()
                            for value in walk_strings(parameters)), parameters)
        modes = [value for key, value in walk_items(parameters)
                 if key.lower() == "mode" or key.lower().endswith("mode")]
        self.assertTrue(any(isinstance(value, str) and "material" in value.lower()
                            for value in modes), modes)
        self.assertTrue(any(isinstance(value, str) and value == material_name
                            for _key, value in walk_items(parameters)), parameters)
        numbers = list(walk_numbers(parameters))
        self.assertTrue(any(number == material_id for number in numbers), numbers)
        etch = operation == "etch"
        signed_directional = (1.0 if etch else -1.0) * directional
        signed_isotropic = (-1.0 if etch else 1.0) * isotropic
        self.assertTrue(any(abs(number - signed_directional) < 1e-9 for number in numbers),
                        (signed_directional, numbers))
        if isotropic > 0.0:
            self.assertTrue(any(abs(number - signed_isotropic) < 1e-9 for number in numbers),
                            (signed_isotropic, numbers))
        if etch and isotropic > 0.0:
            self.assertTrue(any(number < 0.0 for number in numbers), numbers)

    async def test_2d_etch_matches_the_native_material_rate_reference(self):
        value = directional_project(2, "trench")
        value["steps"] = [material_step(
            "etch", direction=[0.0, -1.0, 0.0],
            rates={"Si": directional_material_rate(0.3, 0.05)},
            default=directional_material_rate(0.0, 0.0), duration=0.3)]
        run_id, info, _reference = await self.compare(value, "directional-material-etch-2d")
        parameters = info["actualExecution"][-1]["actualParameters"]
        self.assert_material_metadata(parameters, operation="etch", directional=0.3, isotropic=0.05)

        initial = self.snapshot(run_id, "initial")
        final = self.snapshot(run_id, "step_dir_material")
        axis = vertical_axis(2)
        floor_before = exposed_floor(initial, 10, axis, False)
        floor_after = exposed_floor(final, 10, axis, False)
        self.assertIsNotNone(floor_before)
        self.assertIsNotNone(floor_after)
        self.assertLess(floor_after, floor_before - 0.01,
                        "a positive directional rate with a downward direction must etch the Si surface")
        self.assertAlmostEqual(material_top(final, 0, axis),
                               material_top(initial, 0, axis), places=6,
                               msg="default rate 0 must protect unlisted Mask")

        replay = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        self.assertEqual(replay["normalizedProject"]["steps"][0]["materialRates"],
                         value["steps"][0]["materialRates"])
        response = await asgi_request("/api/runs/replay", method="POST", payload=replay)
        self.assertEqual(response[0], 202, response[2])
        replay_id = json_body(response)["runId"]
        await self.wait(replay_id)
        repeated = self.snapshot(replay_id, "step_dir_material")
        for field in ("points", "lines", "cellData", "materialIdsByLevelSetIndex"):
            self.assertEqual(final[field], repeated[field], field)

    async def test_3d_deposit_matches_the_native_material_rate_reference(self):
        value = directional_project(3, "trench")
        value["steps"] = [material_step(
            "deposit", direction=[0.0, 0.0, -1.0], deposit_material="SiO2",
            rates={"Si": directional_material_rate(0.0, 0.2)},
            default=directional_material_rate(0.0, 0.0), duration=0.3)]
        run_id, info, _reference = await self.compare(value, "directional-material-deposit-3d")
        parameters = info["actualExecution"][-1]["actualParameters"]
        self.assert_material_metadata(parameters, operation="deposit", directional=0.0, isotropic=0.2)

        initial = self.snapshot(run_id, "initial")
        final = self.snapshot(run_id, "step_dir_material")
        axis = vertical_axis(3)
        film_floor = exposed_floor(final, 30, axis, True)
        film_top = material_top(final, 30, axis)
        self.assertIsNotNone(film_floor, "the deposited SiO2 film is missing")
        self.assertIsNotNone(film_top, "the deposited SiO2 film has no surface cells")
        self.assertGreater(film_floor, -1.2 + 0.001,
                           "the listed Si isotropic rate did not grow the deposited SiO2 film")
        self.assertGreater(film_top, film_floor,
                           "the deposited SiO2 film did not gain any thickness")
        self.assertAlmostEqual(material_top(final, 0, axis),
                               material_top(initial, 0, axis), places=6,
                               msg="default rate 0 must protect unlisted Mask")
        self.assertTrue(all(math.isfinite(float(number)) for point in final["points"]
                            for number in point))

    async def test_api_rejects_zero_motion_and_mixed_modes(self):
        cases = []
        value = directional_project(2)
        value["steps"] = [material_step(
            rates={"Si": directional_material_rate(0.0, 0.0)})]
        cases.append(("zero motion", value))
        value = directional_project(2)
        value["steps"] = [material_step(
            directionalRateMagnitude=0.1)]
        cases.append(("scalar field together with materialRates", value))
        for label, value in cases:
            with self.subTest(label=label):
                response = await asgi_request(
                    "/api/runs", method="POST",
                    payload={"kind": "simulate", "project": value})
                self.assertEqual(response[0], 422, (label, response[2]))


if __name__ == "__main__":
    unittest.main()
