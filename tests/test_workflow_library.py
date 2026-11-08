"""P4-3 local team library: exact module revisions, re-plannable experiment templates."""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
import uuid

os.environ.setdefault("WORKBENCH_PORT", "8767")

from pydantic import ValidationError  # noqa: E402

from backend import storage  # noqa: E402
from backend.main import app  # noqa: E402
from backend.workflow.adoption import adopt_project  # noqa: E402
from backend.workflow.experiments import create_experiment_document  # noqa: E402
from backend.workflow.library import (  # noqa: E402
    MAX_LIBRARY_DEFINITIONS,
    LibraryBundleV1,
    LibraryExperimentTemplateV1,
    LibraryExportRequestV1,
    LibraryModuleV1,
    _bundle_id_for,
    build_bundle,
    import_bundle,
)
from backend.workflow.metrology import MetricDefinitionV1, MetrologyRecipeV1  # noqa: E402
from backend.workflow.modules import (  # noqa: E402
    FlowDefinitionV2,
    ModuleV1,
    expand_flow_definition,
    to_flow_revision,
)
from backend.workflow.store import (  # noqa: E402
    SCHEMA_VERSION,
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowStore,
    WorkflowValidation,
)
from backend.workflow.templates import FlowTemplateV1  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402

SOLVER = {
    "timeStepRatio": 0.25,
    "raysPerPoint": 32,
    "cpuThreads": 1,
    "random": {"mode": "fixed", "seed": 7},
    "enginePreference": "cpu",
    "timeoutSeconds": 30,
}


def project() -> dict:
    return {
        "schemaVersion": 7,
        "name": "p4-3 library",
        "dimension": 2,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.2, "xExtent": 4.0, "xBoundary": "reflective"},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [etch_step("step_project_01")],
        "solver": SOLVER,
        "view": {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True},
    }


def etch_step(step_id: str, duration: float = 0.1) -> dict:
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


def module_document(module_id: str, duration: float) -> ModuleV1:
    return ModuleV1.model_validate(
        {
            "schemaVersion": 1,
            "moduleId": module_id,
            "name": f"{module_id} module",
            "description": "",
            "parameters": [],
            "steps": [etch_step("etch_library_a", duration)],
        }
    )


def nested_module_document(
    module_id: str, child_id: str, *, child_revision: int | None = None
) -> ModuleV1:
    nested = {"id": "call_nested", "moduleId": child_id, "parameters": {}}
    if child_revision is not None:
        nested["moduleRevision"] = child_revision
    return ModuleV1.model_validate(
        {
            "schemaVersion": 1,
            "moduleId": module_id,
            "name": f"{module_id} module",
            "description": "",
            "parameters": [],
            "steps": [nested],
        }
    )


def definition_document(
    definition_id: str, flow_id: str, module_id: str, *, pin: bool = True
) -> FlowDefinitionV2:
    return FlowDefinitionV2.model_validate(
        {
            "schemaVersion": 2,
            "definitionId": definition_id,
            "flowId": flow_id,
            "revision": 1,
            "name": "P4-3 definition",
            "description": "",
            "variables": [],
            "nodes": [
                {
                    "kind": "module-call",
                    "instanceId": "call_pinned",
                    "moduleId": module_id,
                    "moduleRevision": 1 if pin else None,
                    "parameters": {},
                }
            ],
            "solver": SOLVER,
        }
    )


def template_document(template_id: str) -> FlowTemplateV1:
    return FlowTemplateV1.model_validate(
        {
            "schemaVersion": 1,
            "templateId": template_id,
            "name": "P4-3 template",
            "description": "",
            "steps": [etch_step("tpl_etch_library", 0.05)],
            "variables": [],
        }
    )


def literal_definition(definition_id: str, index: int) -> FlowDefinitionV2:
    return FlowDefinitionV2.model_validate(
        {
            "schemaVersion": 2,
            "definitionId": definition_id,
            "flowId": f"flow_library_limit_{index:03d}",
            "revision": 1,
            "name": f"limit {index}",
            "description": "",
            "variables": [],
            "nodes": [{"kind": "step", "step": etch_step(f"step_limit_{index:03d}")}],
            "solver": SOLVER,
        }
    )


def compile_definition(store: WorkflowStore, definition: FlowDefinitionV2):
    expanded = expand_flow_definition(definition, store.module_history())
    flow, placements = to_flow_revision(definition, expanded)
    return adopt_project(
        project(),
        flow_id=definition.flowId,
        steps=flow.steps,
        revision=definition.revision,
        placements=placements,
        flow_paths={step.authoredStepId: step.flowPath for step in expanded.steps},
        module_revisions=[item.model_dump(mode="json") for item in expanded.moduleRevisions],
    )[2]


class LibraryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.suffix = uuid.uuid4().hex[:8]
        self.root = storage.VAR / f"test-library-{self.suffix}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.module_id = f"mod_lib_{self.suffix}"
        self.definition_id = f"def_lib_{self.suffix}"
        self.flow_id = f"flow_lib_{self.suffix}"
        self.template_id = f"tpl_lib_{self.suffix}"
        self.recipe_id = f"recipe_lib_{self.suffix}"
        self.metric_id = f"metric_lib_{self.suffix}"
        self.source = WorkflowStore(self.root / "source.sqlite3")
        self.source.initialize()
        self._seed_source()

    def _seed_source(self) -> None:
        self.source.save_module(module_document(self.module_id, 0.1))
        self.source.save_module(module_document(self.module_id, 0.2))
        self.source.save_flow_definition(
            definition_document(self.definition_id, self.flow_id, self.module_id)
        )
        self.source.register_template(template_document(self.template_id))
        self._register_recipe(self.source)
        document = create_experiment_document(
            name="library experiment",
            project=project(),
            flow_id=self.flow_id,
            recipe_id=self.recipe_id,
            recipe_version=1,
            variants=[
                {
                    "parameters": {"step_project_01": {"duration": 0.02}},
                    "replicates": 2,
                    "seed": 111,
                },
                {
                    "parameters": {"step_project_01": {"duration": 0.04}},
                    "replicates": 1,
                    "seed": 222,
                },
            ],
            metric_id=self.metric_id,
        )
        document["design"] = {"method": "manual-design", "replicates": 2, "seed": 111}
        for variant in document["variants"]:
            for replicate in variant["replicates"]:
                replicate.update(
                    {
                        "status": "succeeded",
                        "runId": f"run_{uuid.uuid4().hex[:12]}",
                        "measurementId": f"meas_{uuid.uuid4().hex[:12]}",
                        "attemptCount": 2,
                        "value": 1.25,
                        "unit": "um",
                    }
                )
        self.source.save_experiment(document["experimentId"], document)
        self.experiment_id = document["experimentId"]

    def _register_recipe(self, store: WorkflowStore) -> None:
        store.register_metric(
            MetricDefinitionV1.model_validate(
                {
                    "schemaVersion": 1,
                    "metricId": self.metric_id,
                    "name": "library metric",
                    "unit": "um",
                }
            )
        )
        store.register_recipe(
            MetrologyRecipeV1.model_validate(
                {
                    "schemaVersion": 1,
                    "recipeId": self.recipe_id,
                    "version": 1,
                    "metricId": self.metric_id,
                    "name": "library recipe",
                    "dimension": 2,
                    "method": "etch_depth",
                    "parameters": {"kind": "etch_depth", "x": 0, "referenceHeight": 0},
                    "material": "Si",
                }
            )
        )

    def _export(self, **overrides) -> LibraryBundleV1:
        request = {
            "name": "team library",
            "description": "P4-3 shared bundle",
            "moduleIds": [self.module_id],
            "definitionIds": [self.definition_id],
            "templateIds": [self.template_id],
            "experimentIds": [self.experiment_id],
        }
        request.update(overrides)
        return build_bundle(self.source, LibraryExportRequestV1.model_validate(request))

    def _add_experiment(self, name: str, seed: int) -> str:
        document = create_experiment_document(
            name=name,
            project=project(),
            flow_id=self.flow_id,
            recipe_id=self.recipe_id,
            recipe_version=1,
            variants=[
                {
                    "parameters": {"step_project_01": {"duration": 0.03}},
                    "replicates": 1,
                    "seed": seed,
                }
            ],
            metric_id=self.metric_id,
        )
        self.source.save_experiment(document["experimentId"], document)
        return document["experimentId"]

    def _target(self) -> WorkflowStore:
        store = WorkflowStore(self.root / "target.sqlite3")
        store.initialize()
        self._register_recipe(store)
        return store

    def test_export_carries_pinned_and_current_revision_without_run_state(self):
        bundle = self._export()

        self.assertTrue(bundle.bundleId.startswith("lib_"))
        entries = {(entry.module.moduleId, entry.revision): entry for entry in bundle.modules}
        self.assertEqual(set(entries), {(self.module_id, 1), (self.module_id, 2)})
        self.assertEqual(entries[(self.module_id, 1)].module.steps[0]["duration"], 0.1)
        self.assertEqual(entries[(self.module_id, 2)].module.steps[0]["duration"], 0.2)
        self.assertEqual(bundle.definitions[0].definitionId, self.definition_id)
        self.assertEqual(bundle.templates[0].templateId, self.template_id)

        template = bundle.experimentTemplates[0]
        self.assertEqual(template.kind, "manual")
        self.assertEqual(template.design["seed"], 111)
        self.assertEqual([replicate.seed for replicate in template.variants[0].replicates], [111, 112])
        self.assertEqual([replicate.seed for replicate in template.variants[1].replicates], [222])
        self.assertEqual(
            template.variants[0].parameters, {"step_project_01": {"duration": 0.02}}
        )
        serialized = json.dumps(template.model_dump(mode="json"))
        for forbidden in (
            "runId",
            "measurementId",
            "segmentId",
            "routeId",
            "status",
            "attemptCount",
            "startedAt",
            "finishedAt",
            "value",
        ):
            self.assertNotIn(f'"{forbidden}"', serialized)

        # Content address is stable and ignores the export timestamp.
        rebuilt = build_bundle(self.source, LibraryExportRequestV1.model_validate({
            "name": "team library",
            "description": "P4-3 shared bundle",
            "moduleIds": [self.module_id],
            "definitionIds": [self.definition_id],
            "templateIds": [self.template_id],
            "experimentIds": [self.experiment_id],
        }))
        self.assertEqual(rebuilt.bundleId, bundle.bundleId)

    def test_repeated_export_is_idempotent_and_canonically_ordered(self):
        # Same content exported twice must keep one bundle id *and* one stored
        # createdAt. createdAt is deliberately outside the content identity.
        request = LibraryExportRequestV1.model_validate(
            {
                "name": "team library",
                "description": "P4-3 shared bundle",
                "moduleIds": [self.module_id],
                "definitionIds": [self.definition_id],
                "templateIds": [self.template_id],
                "experimentIds": [self.experiment_id],
            }
        )
        first = build_bundle(self.source, request, created_at="2026-10-30T00:00:00+00:00")
        second = build_bundle(self.source, request, created_at="2026-10-30T00:05:00+00:00")
        self.assertEqual(first.bundleId, second.bundleId)
        stored_first = self.source.save_library_bundle(first)
        stored_second = self.source.save_library_bundle(second)
        self.assertEqual(stored_first["createdAt"], "2026-10-30T00:00:00+00:00")
        self.assertEqual(stored_second["createdAt"], stored_first["createdAt"])
        rows = [
            row
            for row in self.source.list_library_bundles()
            if row["bundleId"] == first.bundleId
        ]
        self.assertEqual(len(rows), 1)

        # Experiment templates have no id, so their order must be derived from
        # content rather than from the store's listing order. Two requests that
        # select the same experiments must serialize identically.
        second_experiment = self._add_experiment("second library experiment", 333)
        forward = self._export(
            experimentIds=[self.experiment_id, second_experiment]
        )
        backward = self._export(
            experimentIds=[second_experiment, self.experiment_id]
        )
        self.assertEqual(forward.bundleId, backward.bundleId)
        self.assertEqual(
            [template.model_dump(mode="json") for template in forward.experimentTemplates],
            [template.model_dump(mode="json") for template in backward.experimentTemplates],
        )
        self.assertEqual(
            [template.name for template in forward.experimentTemplates],
            [template.name for template in backward.experimentTemplates],
        )

    def test_reject_reimport_is_idempotent_when_experiment_order_changes(self):
        second_experiment = self._add_experiment("second library experiment", 444)
        bundle = self._export(
            experimentIds=[self.experiment_id, second_experiment]
        )
        self.assertEqual(len(bundle.experimentTemplates), 2)

        # A recipient may re-serialize the same content with the experiment
        # array reversed; bundleId is content-addressed and ignores order.
        reordered = bundle.model_copy(deep=True)
        reordered.experimentTemplates.reverse()
        self.assertEqual(reordered.bundleId, bundle.bundleId)

        target = self._target()
        first = import_bundle(target, bundle, "reject")
        self.assertEqual(len(first.experimentIds), 2)
        reordered_import = import_bundle(target, reordered, "reject")
        self.assertEqual(reordered_import.experimentIds, [])
        self.assertEqual(
            sorted(
                item.split(":", 2)[2]
                for item in reordered_import.skipped
                if item.startswith("experiment:")
            ),
            sorted(first.experimentIds),
        )
        imported_names = sorted(
            target.get_experiment(experiment_id)["name"]
            for experiment_id in first.experimentIds
        )
        self.assertEqual(
            imported_names,
            ["library experiment", "second library experiment"],
        )

    def test_import_into_second_store_preserves_revisions_and_compiles(self):
        bundle = self._export()
        target = self._target()
        result = import_bundle(target, bundle, "reject")

        self.assertEqual(result.definitionIds, [self.definition_id])
        self.assertEqual(result.templateIds, [self.template_id])
        self.assertEqual(len(result.experimentIds), 1)
        self.assertEqual(
            [row.model_dump() for row in result.modules],
            [
                {"moduleId": self.module_id, "revision": 1, "status": "imported"},
                {"moduleId": self.module_id, "revision": 2, "status": "imported"},
            ],
        )
        self.assertEqual(target.current_module_revision(self.module_id), 2)
        self.assertEqual(target.get_module(self.module_id, 1).steps[0]["duration"], 0.1)
        self.assertEqual(target.get_module(self.module_id, 2).steps[0]["duration"], 0.2)

        source_compile = compile_definition(
            self.source, self.source.get_flow_definition(self.definition_id)
        )
        target_compile = compile_definition(
            target, target.get_flow_definition(self.definition_id)
        )
        self.assertEqual(
            [step.id for step in target_compile.project.steps],
            [step.id for step in source_compile.project.steps],
        )
        self.assertEqual(
            target_compile.compileRecord.moduleRevisions,
            source_compile.compileRecord.moduleRevisions,
        )
        self.assertEqual(
            [(item.moduleId, item.revision) for item in target_compile.compileRecord.moduleRevisions],
            [(self.module_id, 1)],
        )

        imported_template = target.get_template(self.template_id)
        self.assertEqual(imported_template.steps[0]["duration"], 0.05)

        imported = target.get_experiment(result.experimentIds[0])
        self.assertEqual(imported["status"], "planned")
        self.assertEqual(imported["kind"], "manual")
        self.assertEqual(imported["recipeId"], self.recipe_id)
        self.assertEqual(imported["design"], {"method": "manual-design", "replicates": 2, "seed": 111})
        self.assertEqual(len(imported["variants"]), 2)
        self.assertEqual(
            imported["variants"][0]["parameters"],
            {"step_project_01": {"duration": 0.02}},
        )
        self.assertEqual(
            [replicate["seed"] for replicate in imported["variants"][0]["replicates"]], [111, 112]
        )
        self.assertEqual(
            [replicate["seed"] for replicate in imported["variants"][1]["replicates"]], [222]
        )
        for variant in imported["variants"]:
            for replicate in variant["replicates"]:
                self.assertEqual(replicate["status"], "planned")
                self.assertEqual(replicate["attemptCount"], 0)
                self.assertNotIn("runId", replicate)
                self.assertNotIn("measurementId", replicate)
                self.assertNotIn("value", replicate)
        self.assertEqual(imported["librarySource"]["bundleId"], bundle.bundleId)
        self.assertRegex(imported["librarySource"]["templateHash"], r"^[a-f0-9]{64}$")

        # The imported experiment must be runnable from its own project
        # snapshot: run-state and binding fields are reset, so compiling
        # re-derives geometry instead of replaying the source binding.
        self.assertIsNone(imported["binding"])
        self.assertIsNone(imported["bindingId"])
        self.assertIsNone(imported["baseCompileId"])
        flow, _binding, compiled = adopt_project(
            imported["project"], flow_id=imported["flowId"]
        )
        self.assertEqual(flow.flowId, imported["flowId"])
        self.assertTrue(compiled.project.steps)

    def test_reject_conflicts_clone_renames_and_repeat_import_is_idempotent(self):
        # A later source revision must still clone from revision 1.
        source_definition = self.source.get_flow_definition(self.definition_id)
        self.source.save_flow_definition(
            source_definition.model_copy(update={"revision": 2, "name": "source revision two"})
        )
        bundle = self._export()
        target = self._target()
        first = import_bundle(target, bundle, "reject")
        self.assertEqual(len(first.experimentIds), 1)
        self.assertEqual(target.get_flow_definition(self.definition_id).revision, 2)

        repeated = import_bundle(target, bundle, "reject")
        self.assertEqual(repeated.definitionIds, [])
        self.assertEqual(repeated.templateIds, [])
        self.assertEqual(repeated.experimentIds, [])
        self.assertEqual(
            {row.status for row in repeated.modules}, {"existing"}
        )
        self.assertIn(f"definition:{self.definition_id}", repeated.skipped)
        self.assertIn(f"template:{self.template_id}", repeated.skipped)
        self.assertTrue(any(item.startswith("experiment:0:") for item in repeated.skipped))

        # A local edit under the same definition id makes reject explicit.
        local = target.get_flow_definition(self.definition_id)
        target.save_flow_definition(
            local.model_copy(update={"revision": 3, "name": "locally changed"})
        )
        with self.assertRaises(WorkflowConflict) as context:
            import_bundle(target, bundle, "reject")
        self.assertIn(self.definition_id, str(context.exception))

        cloned = import_bundle(target, bundle, "clone")
        self.assertEqual(len(cloned.definitionIds), 1)
        self.assertEqual(len(cloned.templateIds), 1)
        self.assertEqual(len(cloned.experimentIds), 1)
        new_definition_id = cloned.definitionIds[0]
        new_template_id = cloned.templateIds[0]
        self.assertNotEqual(new_definition_id, self.definition_id)
        self.assertNotEqual(new_template_id, self.template_id)
        self.assertTrue(new_definition_id.startswith(self.definition_id))
        self.assertTrue(new_template_id.startswith(self.template_id))
        cloned_definition = target.get_flow_definition(new_definition_id)
        self.assertEqual(cloned_definition.revision, 1)
        self.assertEqual(cloned_definition.nodes[0].moduleId, self.module_id)
        self.assertEqual(cloned_definition.nodes[0].moduleRevision, 1)
        self.assertEqual(target.get_template(new_template_id).templateId, new_template_id)
        self.assertEqual(
            {rename.kind for rename in cloned.renames}, {"definition", "template"}
        )

        cloned_again = import_bundle(target, bundle, "clone")
        self.assertNotEqual(cloned_again.definitionIds[0], new_definition_id)
        self.assertNotEqual(cloned_again.templateIds[0], new_template_id)
        self.assertNotEqual(cloned_again.experimentIds[0], cloned.experimentIds[0])

    def test_import_never_downgrades_a_newer_local_module_revision(self):
        bundle = self._export()  # carries revisions 1 and 2
        target = self._target()
        for duration in (0.1, 0.2, 0.3):
            target.save_module(module_document(self.module_id, duration))
        self.assertEqual(target.current_module_revision(self.module_id), 3)

        result = import_bundle(target, bundle, "reject")
        self.assertEqual({row.status for row in result.modules}, {"existing"})
        self.assertEqual(target.current_module_revision(self.module_id), 3)
        self.assertEqual(target.get_module(self.module_id).steps[0]["duration"], 0.3)
        # Exact revision retention still resolves the definition's pin.
        expanded = expand_flow_definition(
            target.get_flow_definition(self.definition_id), target.module_history()
        )
        self.assertEqual([step.moduleRevision for step in expanded.steps], [1])
        self.assertEqual(expanded.steps[0].step["duration"], 0.1)

    def test_unpinned_definition_rejects_silent_module_drift(self):
        unpinned_id = f"def_lib_unpinned_{self.suffix}"
        flow_id = f"flow_lib_unpinned_{self.suffix}"
        self.source.save_flow_definition(
            definition_document(unpinned_id, flow_id, self.module_id, pin=False)
        )
        bundle = build_bundle(
            self.source,
            LibraryExportRequestV1.model_validate(
                {
                    "name": "unpinned bundle",
                    "moduleIds": [self.module_id],
                    "definitionIds": [unpinned_id],
                    "templateIds": [],
                    "experimentIds": [],
                }
            ),
        )
        target = self._target()
        for duration in (0.1, 0.2, 0.3):
            target.save_module(module_document(self.module_id, duration))
        self.assertEqual(target.current_module_revision(self.module_id), 3)

        with self.assertRaises(WorkflowConflict) as context:
            import_bundle(target, bundle, "reject")
        message = str(context.exception)
        self.assertIn("unpinned", message)
        self.assertIn(f"{self.module_id}@2->3", message)
        with self.assertRaises(WorkflowNotFound):
            target.get_flow_definition(unpinned_id)
        self.assertEqual(target.current_module_revision(self.module_id), 3)

    def test_unpinned_nested_module_call_rejects_silent_drift(self):
        inner_id = f"mod_lib_inner_{self.suffix}"
        outer_id = f"mod_lib_outer_{self.suffix}"
        self.source.save_module(module_document(inner_id, 0.1))
        self.source.save_module(module_document(inner_id, 0.2))
        self.source.save_module(
            ModuleV1.model_validate(
                {
                    "schemaVersion": 1,
                    "moduleId": outer_id,
                    "name": "outer module",
                    "description": "",
                    "parameters": [],
                    "steps": [
                        {"id": "call_inner", "moduleId": inner_id, "parameters": {}}
                    ],
                }
            )
        )
        bundle = build_bundle(
            self.source,
            LibraryExportRequestV1.model_validate(
                {
                    "name": "nested unpinned bundle",
                    "moduleIds": [outer_id],
                    "definitionIds": [],
                    "templateIds": [],
                    "experimentIds": [],
                }
            ),
        )
        target = self._target()
        for duration in (0.1, 0.2, 0.3):
            target.save_module(module_document(inner_id, duration))

        with self.assertRaises(WorkflowConflict) as context:
            import_bundle(target, bundle, "reject")
        message = str(context.exception)
        self.assertIn("unpinned", message)
        self.assertIn(f"{outer_id}", message)
        self.assertIn(f"{inner_id}@2->3", message)
        self.assertFalse(target.module_revision_exists(outer_id, 1))

    def test_existing_parent_and_definition_still_reject_nested_unpinned_drift(self):
        child_id = f"mod_lib_child_{self.suffix}"
        parent_id = f"mod_lib_parent_{self.suffix}"
        definition_id = f"def_lib_parent_{self.suffix}"
        flow_id = f"flow_lib_parent_{self.suffix}"
        self.source.save_module(module_document(child_id, 0.1))
        self.source.save_module(nested_module_document(parent_id, child_id))
        self.source.save_flow_definition(
            definition_document(definition_id, flow_id, parent_id)
        )
        bundle = build_bundle(
            self.source,
            LibraryExportRequestV1.model_validate(
                {
                    "name": "existing parent bundle",
                    "moduleIds": [],
                    "definitionIds": [definition_id],
                    "templateIds": [],
                    "experimentIds": [],
                }
            ),
        )

        target = self._target()
        # The parent revision and the definition are already identical locally,
        # so the old preflight marked both as "no update needed" and skipped the
        # parent's unpinned child call.
        target.save_module(module_document(child_id, 0.1))
        target.save_module(module_document(child_id, 0.2))
        target.save_module(nested_module_document(parent_id, child_id))
        target.save_flow_definition(
            definition_document(definition_id, flow_id, parent_id)
        )
        self.assertEqual(target.current_module_revision(parent_id), 1)

        with self.assertRaises(WorkflowConflict) as context:
            import_bundle(target, bundle, "reject")
        message = str(context.exception)
        self.assertIn("unpinned", message)
        self.assertIn(parent_id, message)
        self.assertIn(f"{child_id}@1->2", message)
        self.assertEqual(target.current_module_revision(child_id), 2)

    def test_explicit_nested_pin_is_not_treated_as_drift(self):
        child_id = f"mod_lib_child_pin_{self.suffix}"
        parent_id = f"mod_lib_parent_pin_{self.suffix}"
        definition_id = f"def_lib_parent_pin_{self.suffix}"
        flow_id = f"flow_lib_parent_pin_{self.suffix}"
        self.source.save_module(module_document(child_id, 0.1))
        self.source.save_module(module_document(child_id, 0.2))
        self.source.save_module(
            nested_module_document(parent_id, child_id, child_revision=1)
        )
        self.source.save_flow_definition(
            definition_document(definition_id, flow_id, parent_id)
        )
        bundle = build_bundle(
            self.source,
            LibraryExportRequestV1.model_validate(
                {
                    "name": "explicit nested pin bundle",
                    "moduleIds": [],
                    "definitionIds": [definition_id],
                    "templateIds": [],
                    "experimentIds": [],
                }
            ),
        )
        # The explicit child pin keeps the bundle to child revision 1 only.
        self.assertNotIn(
            (child_id, 2),
            {(entry.module.moduleId, entry.revision) for entry in bundle.modules},
        )

        target = self._target()
        target.save_module(module_document(child_id, 0.1))
        target.save_module(module_document(child_id, 0.2))
        target.save_module(
            nested_module_document(parent_id, child_id, child_revision=1)
        )
        target.save_flow_definition(
            definition_document(definition_id, flow_id, parent_id)
        )
        result = import_bundle(target, bundle, "reject")
        self.assertEqual(result.definitionIds, [])
        self.assertIn(f"definition:{definition_id}", result.skipped)
        self.assertEqual(target.current_module_revision(child_id), 2)
        expanded = expand_flow_definition(
            target.get_flow_definition(definition_id), target.module_history()
        )
        self.assertEqual([step.moduleRevision for step in expanded.steps], [1])
        self.assertEqual(expanded.steps[0].step["duration"], 0.1)

    def test_module_conflict_requires_identical_content_even_for_clone(self):
        bundle = self._export()
        target = self._target()
        target.save_module(module_document(self.module_id, 0.7))
        for policy in ("reject", "clone"):
            with self.assertRaises(WorkflowConflict) as context:
                import_bundle(target, bundle, policy)
            self.assertIn(self.module_id, str(context.exception))

    def test_missing_recipe_is_an_explicit_conflict(self):
        bundle = self._export()
        target = WorkflowStore(self.root / "no-recipe.sqlite3")
        target.initialize()
        with self.assertRaises(WorkflowConflict) as context:
            import_bundle(target, bundle, "reject")
        self.assertIn(self.recipe_id, str(context.exception))
        self.assertIn("recipe", str(context.exception))

    def test_limits_duplicates_and_mismatched_bundle_id_are_rejected(self):
        base = self._export()
        with self.assertRaises(WorkflowValidation):
            build_bundle(
                self.source,
                LibraryExportRequestV1.model_validate({
                    "name": "duplicate request",
                    "definitionIds": [self.definition_id, self.definition_id],
                }),
            )
        with self.assertRaises(WorkflowNotFound):
            build_bundle(
                self.source,
                LibraryExportRequestV1.model_validate({
                    "name": "missing module",
                    "moduleIds": [f"mod_missing_{self.suffix}"],
                    "definitionIds": [],
                    "templateIds": [],
                    "experimentIds": [],
                }),
            )

        too_many = [
            literal_definition(f"def_limit_{self.suffix}_{index:03d}", index).model_dump(
                mode="json"
            )
            for index in range(MAX_LIBRARY_DEFINITIONS + 1)
        ]
        payload = base.model_dump(mode="json")
        payload["definitions"] = too_many
        with self.assertRaises(ValidationError):
            LibraryBundleV1.model_validate(payload)

        duplicate = base.model_dump(mode="json")
        duplicate["definitions"] = [duplicate["definitions"][0], duplicate["definitions"][0]]
        with self.assertRaises(ValidationError):
            LibraryBundleV1.model_validate(duplicate)

        mismatched = base.model_dump(mode="json")
        mismatched["bundleId"] = "lib_" + "0" * 24
        with self.assertRaises(ValidationError):
            LibraryBundleV1.model_validate(mismatched)

    def test_store_schema_is_current_and_bundles_roundtrip(self):
        self.assertEqual(SCHEMA_VERSION, "13")
        self.assertEqual(self.source.schema_version(), SCHEMA_VERSION)
        bundle = self._export()
        stored = self.source.save_library_bundle(bundle)
        self.assertEqual(stored["bundleId"], bundle.bundleId)
        self.assertEqual(self.source.get_library_bundle(bundle.bundleId)["name"], "team library")
        summaries = self.source.list_library_bundles()
        self.assertTrue(any(row["bundleId"] == bundle.bundleId for row in summaries))
        # Saving identical content is idempotent.
        self.source.save_library_bundle(bundle)

    def test_v7_store_gains_the_library_table_on_initialize(self):
        path = self.root / "v7.sqlite3"
        store = WorkflowStore(path)
        store.initialize()
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE library_bundles")
            connection.execute(
                "UPDATE schema_meta SET value = '7' WHERE key = 'schema_version'"
            )
        upgraded = WorkflowStore(path)
        upgraded.initialize()
        self.assertEqual(upgraded.schema_version(), SCHEMA_VERSION)
        self.assertEqual(upgraded.list_library_bundles(), [])

    def test_failed_preflight_leaves_no_partial_writes(self):
        bundle = self._export()
        target = self._target()
        target.register_template(
            template_document(self.template_id).model_copy(
                update={"name": "local only template"}
            )
        )
        with self.assertRaises(WorkflowConflict) as context:
            import_bundle(target, bundle, "reject")
        self.assertIn(f"template:{self.template_id}", str(context.exception))
        # Nothing from the bundle may be written before the conflict is raised.
        self.assertFalse(target.module_revision_exists(self.module_id, 1))
        self.assertFalse(target.module_revision_exists(self.module_id, 2))
        with self.assertRaises(WorkflowNotFound):
            target.current_module_revision(self.module_id)
        self.assertEqual(target.get_template(self.template_id).name, "local only template")


class WorkflowLibraryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        self.suffix = uuid.uuid4().hex[:8]
        self.module_id = f"mod_libapi_{self.suffix}"
        self.definition_id = f"def_libapi_{self.suffix}"
        self.template_id = f"tpl_libapi_{self.suffix}"
        self.recipe_id = f"recipe_libapi_{self.suffix}"
        self.metric_id = f"metric_libapi_{self.suffix}"

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    async def post(self, path: str, payload: dict):
        return await asgi_request(path, method="POST", payload=payload)

    async def get(self, path: str):
        return await asgi_request(path, method="GET")

    async def _seed(self) -> str:
        steps = [
            ("/api/v1/workflow/metrics", {
                "schemaVersion": 1, "metricId": self.metric_id, "name": "lib api", "unit": "um",
            }),
            ("/api/v1/workflow/metrology-recipes", {
                "schemaVersion": 1, "recipeId": self.recipe_id, "version": 1,
                "metricId": self.metric_id, "name": "lib api recipe", "dimension": 2,
                "method": "etch_depth",
                "parameters": {"kind": "etch_depth", "x": 0, "referenceHeight": 0},
                "material": "Si",
            }),
            ("/api/v1/workflow/modules", module_document(self.module_id, 0.1).model_dump(mode="json")),
            ("/api/v1/workflow/modules", module_document(self.module_id, 0.2).model_dump(mode="json")),
            (
                "/api/v1/workflow/flows/definitions",
                definition_document(self.definition_id, f"flow_libapi_{self.suffix}", self.module_id).model_dump(mode="json"),
            ),
            ("/api/v1/workflow/templates", template_document(self.template_id).model_dump(mode="json")),
        ]
        for path, payload in steps:
            response = await self.post(path, payload)
            self.assertEqual(response[0], 201, response[2])
        experiment = await self.post(
            "/api/v1/workflow/experiments",
            {
                "name": "lib api experiment",
                "project": project(),
                "flowId": f"flow_libapi_{self.suffix}",
                "recipeId": self.recipe_id,
                "recipeVersion": 1,
                "variants": [
                    {"parameters": {"step_project_01": {"duration": 0.02}}, "replicates": 2, "seed": 31},
                    {"parameters": {"step_project_01": {"duration": 0.03}}, "replicates": 1, "seed": 32},
                ],
            },
        )
        self.assertEqual(experiment[0], 201, experiment[2])
        return json_body(experiment)["experimentId"]

    async def test_export_list_get_import_and_openapi(self):
        experiment_id = await self._seed()
        export_payload = {
            "name": "api team library",
            "description": "P4-3",
            "moduleIds": [self.module_id],
            "definitionIds": [self.definition_id],
            "templateIds": [self.template_id],
            "experimentIds": [experiment_id],
        }
        exported = await self.post(
            "/api/v1/workflow/library/export", export_payload
        )
        self.assertEqual(exported[0], 201, exported[2])
        bundle = json_body(exported)
        self.assertTrue(bundle["bundleId"].startswith("lib_"))
        self.assertEqual(
            {(entry["module"]["moduleId"], entry["revision"]) for entry in bundle["modules"]},
            {(self.module_id, 1), (self.module_id, 2)},
        )
        self.assertEqual(
            [replicate["seed"] for replicate in bundle["experimentTemplates"][0]["variants"][0]["replicates"]],
            [31, 32],
        )

        listed = await self.get("/api/v1/workflow/library/bundles")
        self.assertEqual(listed[0], 200, listed[2])
        self.assertIn(
            bundle["bundleId"],
            {item["bundleId"] for item in json_body(listed)["bundles"]},
        )
        fetched = await self.get(f"/api/v1/workflow/library/bundles/{bundle['bundleId']}")
        self.assertEqual(fetched[0], 200, fetched[2])
        self.assertEqual(json_body(fetched)["bundleId"], bundle["bundleId"])

        # Exporting the same content again is idempotent: one catalog entry,
        # same content id and the original createdAt.
        re_exported = await self.post("/api/v1/workflow/library/export", export_payload)
        self.assertEqual(re_exported[0], 201, re_exported[2])
        self.assertEqual(json_body(re_exported)["bundleId"], bundle["bundleId"])
        self.assertEqual(json_body(re_exported)["createdAt"], bundle["createdAt"])
        relisted = await self.get("/api/v1/workflow/library/bundles")
        self.assertEqual(relisted[0], 200, relisted[2])
        self.assertEqual(
            len(
                [
                    item
                    for item in json_body(relisted)["bundles"]
                    if item["bundleId"] == bundle["bundleId"]
                ]
            ),
            1,
        )

        cloned = await self.post(
            f"/api/v1/workflow/library/bundles/{bundle['bundleId']}/import",
            {"policy": "clone"},
        )
        self.assertEqual(cloned[0], 200, cloned[2])
        result = json_body(cloned)
        self.assertEqual(len(result["definitionIds"]), 1)
        self.assertNotEqual(result["definitionIds"][0], self.definition_id)
        self.assertEqual(len(result["experimentIds"]), 1)
        imported_definition = await self.get(
            f"/api/v1/workflow/flows/definitions/{result['definitionIds'][0]}"
        )
        self.assertEqual(imported_definition[0], 200, imported_definition[2])

        direct = await self.post(
            "/api/v1/workflow/library/import",
            {"bundle": bundle, "policy": "clone"},
        )
        self.assertEqual(direct[0], 200, direct[2])
        self.assertNotEqual(
            json_body(direct)["definitionIds"][0], result["definitionIds"][0]
        )

        schema = app.openapi()
        for path in (
            "/api/v1/workflow/library/export",
            "/api/v1/workflow/library/bundles",
            "/api/v1/workflow/library/bundles/{bundle_id}",
            "/api/v1/workflow/library/bundles/{bundle_id}/import",
            "/api/v1/workflow/library/import",
        ):
            self.assertIn(path, schema["paths"])
        for name in (
            "LibraryBundleV1-Input",
            "LibraryBundleV1-Output",
            "LibraryExportRequestV1",
            "LibraryImportResultV1",
            "LibraryBundleListResponse",
        ):
            self.assertIn(name, schema["components"]["schemas"])

    async def test_module_content_conflict_returns_409_and_invalid_bundle_returns_422(self):
        experiment_id = await self._seed()
        exported = await self.post(
            "/api/v1/workflow/library/export",
            {
                "name": "conflict library",
                "moduleIds": [self.module_id],
                "definitionIds": [self.definition_id],
                "templateIds": [self.template_id],
                "experimentIds": [experiment_id],
            },
        )
        self.assertEqual(exported[0], 201, exported[2])
        bundle = json_body(exported)

        # Change retained content at the same (moduleId, revision) and re-seal the
        # content hash. This is the explicit module conflict both policies reject.
        modules = []
        for entry in bundle["modules"]:
            module = ModuleV1.model_validate(entry["module"])
            if entry["revision"] == 1:
                module = module.model_copy(update={"steps": [etch_step("etch_library_a", 0.75)]})
            modules.append(LibraryModuleV1(module=module, revision=entry["revision"]))
        definitions = [FlowDefinitionV2.model_validate(item) for item in bundle["definitions"]]
        templates = [FlowTemplateV1.model_validate(item) for item in bundle["templates"]]
        experiment_templates = [
            LibraryExperimentTemplateV1.model_validate(item)
            for item in bundle["experimentTemplates"]
        ]
        mutated = dict(bundle)
        mutated["modules"] = [item.model_dump(mode="json", exclude_none=True) for item in modules]
        mutated["bundleId"] = _bundle_id_for(
            name=bundle["name"],
            description=bundle.get("description") or "",
            modules=modules,
            definitions=definitions,
            templates=templates,
            experimentTemplates=experiment_templates,
        )
        conflict = await self.post(
            "/api/v1/workflow/library/import",
            {"bundle": mutated, "policy": "reject"},
        )
        self.assertEqual(conflict[0], 409, conflict[2])
        # A rejected import must not register a half-imported bundle.
        listed = await self.get("/api/v1/workflow/library/bundles")
        self.assertEqual(listed[0], 200, listed[2])
        self.assertNotIn(
            mutated["bundleId"],
            {item["bundleId"] for item in json_body(listed)["bundles"]},
        )

        still_conflict = await self.post(
            f"/api/v1/workflow/library/bundles/{bundle['bundleId']}/import",
            {"policy": "reject"},
        )
        self.assertEqual(still_conflict[0], 200, still_conflict[2])

        invalid = dict(bundle)
        invalid["bundleId"] = "lib_" + "0" * 24
        rejected = await self.post(
            "/api/v1/workflow/library/import",
            {"bundle": invalid, "policy": "reject"},
        )
        self.assertEqual(rejected[0], 422, rejected[2])

        duplicate = dict(bundle)
        duplicate["definitions"] = [bundle["definitions"][0], bundle["definitions"][0]]
        rejected = await self.post(
            "/api/v1/workflow/library/import",
            {"bundle": duplicate, "policy": "reject"},
        )
        self.assertEqual(rejected[0], 422, rejected[2])


if __name__ == "__main__":
    unittest.main()
