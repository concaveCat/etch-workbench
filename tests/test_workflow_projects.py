"""Persisted ProjectV7 documents and the built-in library presets (schema 10)."""

from __future__ import annotations

import os
import sqlite3
import unittest
import uuid

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend import storage  # noqa: E402
from backend.main import app  # noqa: E402
from backend.materials import material_catalog  # noqa: E402
from backend.models import ProjectV7  # noqa: E402
from backend.workflow.library_items import (  # noqa: E402
    create_library_item,
    materialize_process_preset,
)
from backend.workflow.library_presets import (  # noqa: E402
    PROCESS_PRESETS,
    material_role,
)
from backend.workflow.store import (  # noqa: E402
    SCHEMA_VERSION,
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowStore,
    WorkflowValidation,
)
from tests.test_api_integration import asgi_request, json_body  # noqa: E402

PROJECTS = "/api/v1/workflow/projects"


def etch_step(step_id: str = "step_etch_01", *, duration: float = 0.1) -> dict:
    return {
        "id": step_id,
        "name": "etch",
        "enabled": True,
        "operation": "etch",
        "model": "isotropic",
        "rateMagnitude": 0.1,
        "duration": duration,
        "rateMode": "uniform",
    }


def deposit_step(step_id: str = "step_dep_01") -> dict:
    return {
        "id": step_id,
        "name": "deposit",
        "enabled": True,
        "operation": "deposit",
        "model": "isotropic",
        "rateMagnitude": 0.05,
        "duration": 0.2,
        "rateMode": "uniform",
        "depositMaterial": "SiO2",
    }


def project(name: str = "persisted 2D", *, steps: list[dict] | None = None) -> dict:
    return {
        "schemaVersion": 7,
        "name": name,
        "dimension": 2,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.2, "xExtent": 4.0, "xBoundary": "reflective"},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [etch_step()] if steps is None else steps,
        "solver": {
            "timeStepRatio": 0.25,
            "raysPerPoint": 32,
            "cpuThreads": 1,
            "random": {"mode": "fixed", "seed": 7},
            "enginePreference": "cpu",
            "timeoutSeconds": 30,
        },
        "view": {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True},
    }


class ProjectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.root = storage.VAR / f"test-projects-{suffix}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = WorkflowStore(self.root / "workflow.sqlite3")
        self.store.initialize(seed=False)

    def test_create_list_get_round_trip(self) -> None:
        record, created = self.store.create_project(project("store 2D"))
        self.assertTrue(created)
        self.assertEqual(record["schemaVersion"], 1)
        self.assertRegex(record["projectId"], r"^proj_[a-f0-9]{24}$")
        self.assertEqual(record["name"], "store 2D")
        self.assertEqual(record["dimension"], 2)
        self.assertEqual(record["stepCount"], 1)
        self.assertEqual(record["createdAt"], record["updatedAt"])

        listed = self.store.list_projects()
        self.assertEqual([row["projectId"] for row in listed], [record["projectId"]])
        self.assertEqual(listed[0], record)

        document = self.store.get_project(record["projectId"])
        self.assertEqual(document["schemaVersion"], 7)
        self.assertEqual(document["name"], "store 2D")
        self.assertEqual([step["id"] for step in document["steps"]], ["step_etch_01"])
        ProjectV7.model_validate(document)

    def test_create_is_idempotent_only_for_identical_content(self) -> None:
        first, created = self.store.create_project(project("idempotent"))
        self.assertTrue(created)
        again, created_again = self.store.create_project(project("idempotent"))
        self.assertFalse(created_again)
        self.assertEqual(again, first)
        self.assertEqual(len(self.store.list_projects()), 1)

        with self.assertRaises(WorkflowConflict):
            self.store.create_project(
                project("idempotent", steps=[etch_step(duration=0.2)])
            )
        # The conflicting create never overwrites the stored document.
        stored = self.store.get_project(first["projectId"])
        self.assertEqual(stored["steps"][0]["duration"], 0.1)
        self.assertEqual(len(self.store.list_projects()), 1)

    def test_update_replaces_steps_and_is_idempotent(self) -> None:
        record, _ = self.store.create_project(
            project("update", steps=[etch_step(), deposit_step()])
        )
        self.assertEqual(record["stepCount"], 2)

        replaced = self.store.update_project(
            record["projectId"], project("update", steps=[etch_step(duration=0.3)])
        )
        self.assertEqual(replaced["stepCount"], 1)
        self.assertEqual(replaced["createdAt"], record["createdAt"])
        document = self.store.get_project(record["projectId"])
        self.assertEqual(len(document["steps"]), 1)
        self.assertEqual(document["steps"][0]["duration"], 0.3)

        repeated = self.store.update_project(
            record["projectId"], project("update", steps=[etch_step(duration=0.3)])
        )
        self.assertEqual(repeated["updatedAt"], replaced["updatedAt"])

    def test_rename_collision_is_a_conflict(self) -> None:
        first, _ = self.store.create_project(project("first"))
        self.store.create_project(project("second"))
        with self.assertRaises(WorkflowConflict):
            self.store.update_project(first["projectId"], project("second"))
        # Same name with new content is a normal update of that project.
        edited = self.store.update_project(
            first["projectId"], project("first", steps=[etch_step(duration=0.4)])
        )
        self.assertEqual(edited["name"], "first")
        # A rename to a free name still works.
        renamed = self.store.update_project(first["projectId"], project("renamed"))
        self.assertEqual(renamed["name"], "renamed")

    def test_missing_project_is_404_and_delete_removes_it(self) -> None:
        missing = f"proj_{uuid.uuid4().hex[:24]}"
        with self.assertRaises(WorkflowNotFound):
            self.store.get_project(missing)
        with self.assertRaises(WorkflowNotFound):
            self.store.update_project(missing, project("missing"))
        with self.assertRaises(WorkflowNotFound):
            self.store.delete_project(missing)

        record, _ = self.store.create_project(project("delete me"))
        self.store.delete_project(record["projectId"])
        with self.assertRaises(WorkflowNotFound):
            self.store.get_project(record["projectId"])
        with self.assertRaises(WorkflowNotFound):
            self.store.delete_project(record["projectId"])
        self.assertEqual(self.store.list_projects(), [])

    def test_invalid_documents_are_rejected(self) -> None:
        cases = (
            {**project(), "schemaVersion": 6},
            {**project(), "schemaVersion": "7"},
            {key: value for key, value in project().items() if key != "steps"},
            {**project(), "bogusField": 1},
            {**project(), "steps": [{**etch_step(), "duration": 0.0}]},
            {**project(), "steps": [etch_step(), etch_step()]},
            {**project(), "name": ""},
            {**project(), "name": "   "},
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(WorkflowValidation):
                    self.store.create_project(case)
        self.assertEqual(self.store.list_projects(), [])

    def test_project_writes_do_not_compile_or_route(self) -> None:
        record, _ = self.store.create_project(project("no side effects"))
        self.store.update_project(record["projectId"], project("no side effects renamed"))
        self.assertEqual(self.store.list_compiles(), [])
        with sqlite3.connect(self.store.path) as connection:
            for table in ("compiles", "routes", "attempts", "segments"):
                (count,) = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                self.assertEqual(count, 0, table)

    def test_schema_9_database_gains_the_projects_table(self) -> None:
        path = self.root / "migrate.sqlite3"
        WorkflowStore(path).initialize(seed=False)
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE projects")
            connection.execute("UPDATE schema_meta SET value = '9' WHERE key = 'schema_version'")
        self.assertNotEqual(SCHEMA_VERSION, "9")

        upgraded = WorkflowStore(path)
        upgraded.initialize(seed=False)
        self.assertEqual(upgraded.schema_version(), SCHEMA_VERSION)
        with sqlite3.connect(path) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        self.assertIn("projects", tables)

        record, created = upgraded.create_project(project("after migration"))
        self.assertTrue(created)
        self.assertEqual(upgraded.get_project(record["projectId"])["name"], "after migration")


class ProjectApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        self.suffix = uuid.uuid4().hex[:8]

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    async def post(self, path: str, payload: dict):
        return await asgi_request(path, method="POST", payload=payload)

    async def get(self, path: str):
        return await asgi_request(path, method="GET")

    async def put(self, path: str, payload: dict):
        return await asgi_request(path, method="PUT", payload=payload)

    async def delete(self, path: str):
        return await asgi_request(path, method="DELETE")

    async def test_project_crud_round_trip(self) -> None:
        name = f"api project {self.suffix}"
        created = await self.post(PROJECTS, project(name))
        self.assertEqual(created[0], 201, created[2])
        record = json_body(created)
        self.assertRegex(record["projectId"], r"^proj_[a-f0-9]{24}$")
        self.assertEqual(record["schemaVersion"], 1)
        self.assertEqual(record["name"], name)
        self.assertEqual(record["dimension"], 2)
        self.assertEqual(record["stepCount"], 1)
        project_id = record["projectId"]

        listed = await self.get(PROJECTS)
        self.assertEqual(listed[0], 200, listed[2])
        self.assertIn(project_id, {row["projectId"] for row in json_body(listed)["projects"]})

        fetched = await self.get(f"{PROJECTS}/{project_id}")
        self.assertEqual(fetched[0], 200, fetched[2])
        document = json_body(fetched)
        self.assertEqual(document["schemaVersion"], 7)
        self.assertEqual(document["name"], name)
        self.assertEqual([step["id"] for step in document["steps"]], ["step_etch_01"])

        updated = await self.put(
            f"{PROJECTS}/{project_id}",
            project(name, steps=[etch_step(duration=0.3), deposit_step()]),
        )
        self.assertEqual(updated[0], 200, updated[2])
        self.assertEqual(json_body(updated)["stepCount"], 2)
        self.assertEqual(json_body(updated)["createdAt"], record["createdAt"])

        # Replacing the document again with one step removes the other step.
        replaced = await self.put(
            f"{PROJECTS}/{project_id}", project(name, steps=[etch_step(duration=0.3)])
        )
        self.assertEqual(replaced[0], 200, replaced[2])
        self.assertEqual(json_body(replaced)["stepCount"], 1)
        fetched = await self.get(f"{PROJECTS}/{project_id}")
        self.assertEqual(len(json_body(fetched)["steps"]), 1)

        repeated = await self.put(
            f"{PROJECTS}/{project_id}", project(name, steps=[etch_step(duration=0.3)])
        )
        self.assertEqual(repeated[0], 200, repeated[2])
        self.assertEqual(json_body(repeated)["updatedAt"], json_body(replaced)["updatedAt"])

        deleted = await self.delete(f"{PROJECTS}/{project_id}")
        self.assertEqual(deleted[0], 204, deleted[2])
        self.assertEqual((await self.get(f"{PROJECTS}/{project_id}"))[0], 404)
        self.assertEqual((await self.delete(f"{PROJECTS}/{project_id}"))[0], 404)

    async def test_create_is_idempotent_and_same_name_conflicts(self) -> None:
        name = f"idempotent api {self.suffix}"
        first = await self.post(PROJECTS, project(name))
        self.assertEqual(first[0], 201, first[2])
        again = await self.post(PROJECTS, project(name))
        self.assertEqual(again[0], 200, again[2])
        self.assertEqual(json_body(again)["projectId"], json_body(first)["projectId"])

        conflict = await self.post(
            PROJECTS, project(name, steps=[etch_step(duration=0.2)])
        )
        self.assertEqual(conflict[0], 409, conflict[2])
        self.assertIn("already exists", json_body(conflict)["detail"])

    async def test_unknown_project_and_invalid_body(self) -> None:
        missing = f"proj_{uuid.uuid4().hex[:24]}"
        self.assertEqual((await self.get(f"{PROJECTS}/{missing}"))[0], 404)
        self.assertEqual((await self.put(f"{PROJECTS}/{missing}", project("ghost")))[0], 404)

        old_schema = await self.post(PROJECTS, {**project("old"), "schemaVersion": 6})
        self.assertEqual(old_schema[0], 422, old_schema[2])
        extra_field = await self.post(PROJECTS, {**project("extra"), "bogus": True})
        self.assertEqual(extra_field[0], 422, extra_field[2])
        no_steps = await self.post(
            PROJECTS, {key: value for key, value in project("empty").items() if key != "steps"}
        )
        self.assertEqual(no_steps[0], 422, no_steps[2])
        bad_step = await self.post(
            PROJECTS, project("bad step", steps=[{**etch_step(), "duration": 0.0}])
        )
        self.assertEqual(bad_step[0], 422, bad_step[2])
        blank_name = await self.post(PROJECTS, project("   "))
        self.assertEqual(blank_name[0], 422, blank_name[2])
        self.assertIn("blank", json_body(blank_name)["detail"])

    async def test_put_rename_collision_is_409(self) -> None:
        first = json_body(await self.post(PROJECTS, project(f"rename a {self.suffix}")))
        await self.post(PROJECTS, project(f"rename b {self.suffix}"))
        conflict = await self.put(
            f"{PROJECTS}/{first['projectId']}", project(f"rename b {self.suffix}")
        )
        self.assertEqual(conflict[0], 409, conflict[2])

    async def test_saving_a_project_creates_no_compile_or_route(self) -> None:
        before_compiles = json_body(await self.get("/api/v1/workflow/compiles"))["compiles"]
        before_routes = json_body(await self.get("/api/v1/workflow/routes"))["routes"]
        record = json_body(await self.post(PROJECTS, project(f"side effects {self.suffix}")))
        after_compiles = json_body(await self.get("/api/v1/workflow/compiles"))["compiles"]
        after_routes = json_body(await self.get("/api/v1/workflow/routes"))["routes"]
        self.assertEqual(len(after_compiles), len(before_compiles))
        self.assertEqual(len(after_routes), len(before_routes))
        self.assertEqual(record["stepCount"], 1)


class LibrarySeedTests(unittest.TestCase):
    def setUp(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.root = storage.VAR / f"test-seed-{suffix}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = WorkflowStore(self.root / "workflow.sqlite3")

    def test_fresh_store_seeds_every_catalog_material(self) -> None:
        self.store.initialize()
        catalog = material_catalog()["materials"]
        items = self.store.list_library_items("material")
        self.assertEqual({item["payload"]["material"] for item in items}, {row["name"] for row in catalog})
        by_name = {item["payload"]["material"]: item for item in items}
        for row in catalog:
            item = by_name[row["name"]]
            self.assertEqual(item["name"], row["name"])
            self.assertEqual(item["payload"]["catalog"], row["category"])
            self.assertEqual(item["payload"]["displayName"], row["name"])
            self.assertEqual(item["payload"]["color"], row["color"])
            self.assertEqual(item["payload"]["role"], material_role(row["name"]))
        self.assertEqual(by_name["Si"]["payload"]["role"], "substrate")
        self.assertEqual(by_name["Mask"]["payload"]["role"], "mask")
        self.assertEqual(by_name["PMMA"]["payload"]["role"], "mask")
        self.assertEqual(by_name["SiO2"]["payload"]["role"], "film")

    def test_process_presets_are_insertable_steps(self) -> None:
        self.store.initialize()
        items = self.store.list_library_items("process")
        self.assertEqual(
            {item["name"] for item in items},
            {"Isotropic Etch", "Isotropic Deposit", "Single Particle Etch"},
        )
        # The list is newest first, and the seed keeps the declared order.
        self.assertEqual(
            [item["name"] for item in items],
            [name for _, name, _, _ in PROCESS_PRESETS],
        )
        by_name = {item["name"]: item for item in items}
        etch = materialize_process_preset(by_name["Isotropic Etch"]["payload"])
        self.assertEqual((etch["operation"], etch["model"]), ("etch", "isotropic"))
        deposit = materialize_process_preset(by_name["Isotropic Deposit"]["payload"])
        self.assertEqual(deposit["operation"], "deposit")
        self.assertEqual(deposit["depositMaterial"], "SiO2")
        particle = materialize_process_preset(by_name["Single Particle Etch"]["payload"])
        self.assertEqual(particle["model"], "singleParticle")
        self.assertEqual(particle["stickingProbability"], 0.5)
        self.assertEqual(particle["sourceExponent"], 1.0)
        for document in (etch, deposit, particle):
            self.assertRegex(document["id"], r"^[A-Za-z0-9_-]{8,64}$")

    def test_flow_and_experiment_categories_stay_empty(self) -> None:
        self.store.initialize()
        self.assertEqual(self.store.count_library_items("flow"), 0)
        self.assertEqual(self.store.count_library_items("experiment"), 0)

    def test_seed_is_idempotent_and_keeps_user_entries(self) -> None:
        self.store.initialize()
        material_ids = {row["itemId"] for row in self.store.list_library_items("material")}
        process_ids = {row["itemId"] for row in self.store.list_library_items("process")}

        user_item = create_library_item(
            category="process",
            name="user etch",
            description="local preset",
            payload={
                "operation": "etch",
                "model": "isotropic",
                "rateMagnitude": 0.2,
                "duration": 1.0,
                "rateMode": "uniform",
            },
        )
        self.store.save_library_item(user_item.model_dump(mode="json"))

        self.store.initialize()
        self.assertEqual(
            {row["itemId"] for row in self.store.list_library_items("material")}, material_ids
        )
        processes = self.store.list_library_items("process")
        self.assertTrue(process_ids <= {row["itemId"] for row in processes})
        self.assertIn(user_item.itemId, {row["itemId"] for row in processes})
        self.assertEqual(self.store.count_library_items("process"), 4)

    def test_seed_only_fills_an_empty_category(self) -> None:
        self.store.initialize(seed=False)
        self.assertEqual(self.store.count_library_items("material"), 0)
        self.assertEqual(self.store.count_library_items("process"), 0)

        user_material = create_library_item(
            category="material",
            name="Only Si",
            payload={
                "catalog": "Silicon",
                "material": "Si",
                "displayName": "Silicon",
                "color": "#c79a08",
                "role": "substrate",
            },
        )
        self.store.save_library_item(user_material.model_dump(mode="json"))

        self.store.initialize()
        # The populated category is left alone; the empty one is seeded.
        self.assertEqual(self.store.count_library_items("material"), 1)
        self.assertEqual(self.store.count_library_items("process"), 3)

    def test_seed_can_be_disabled(self) -> None:
        self.store.initialize(seed=False)
        for category in ("material", "process", "flow", "experiment"):
            self.assertEqual(self.store.count_library_items(category), 0, category)


if __name__ == "__main__":
    unittest.main()
