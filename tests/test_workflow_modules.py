"""P4-1 hierarchical modules, expressions, loops and definition compilation."""

from __future__ import annotations

import os
import sqlite3
import unittest
import uuid

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend import storage  # noqa: E402
from backend.main import app  # noqa: E402
from backend.workflow.adoption import adopt_project  # noqa: E402
from backend.workflow.api import get_store  # noqa: E402
from backend.workflow.compiler import compile_flow  # noqa: E402
from backend.workflow.hashing import source_map_hash  # noqa: E402
from backend.workflow.ids import sha256_hex  # noqa: E402
from backend.workflow.modules import (  # noqa: E402
    MAX_EXPANDED_STEPS,
    MAX_EXPANSION_DEPTH,
    ExpressionError,
    ModuleV1,
    evaluate_expression,
    expand_flow_definition,
    resolve_value,
    to_flow_revision,
)
from backend.workflow.store import SCHEMA_VERSION, WorkflowStore  # noqa: E402
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
        "name": "p4-1 modules",
        "dimension": 2,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.2, "xExtent": 4.0, "xBoundary": "reflective"},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [etch_step("step_project_01")],
        "solver": SOLVER,
        "view": {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True},
    }


def etch_step(step_id: str, **overrides) -> dict:
    step = {
        "id": step_id,
        "name": "etch",
        "enabled": True,
        "operation": "etch",
        "model": "isotropic",
        "rateMagnitude": 0.1,
        "duration": 0.1,
        "rateMode": "uniform",
    }
    step.update(overrides)
    return step


def module(module_id: str, steps: list[dict], parameters: list[dict] | None = None) -> dict:
    return {
        "schemaVersion": 1,
        "moduleId": module_id,
        "name": f"{module_id} module",
        "description": "",
        "parameters": parameters or [],
        "steps": steps,
    }


def definition(
    definition_id: str,
    flow_id: str,
    nodes: list[dict],
    variables: list[dict] | None = None,
    revision: int = 1,
) -> dict:
    return {
        "schemaVersion": 2,
        "definitionId": definition_id,
        "flowId": flow_id,
        "revision": revision,
        "name": "P4-1 definition",
        "description": "",
        "variables": variables or [],
        "nodes": nodes,
        "solver": SOLVER,
    }


class WorkflowModuleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        self.suffix = uuid.uuid4().hex[:8]

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    async def post(self, path: str, payload: dict):
        return await asgi_request(path, method="POST", payload=payload)

    async def save_module(self, payload: dict) -> dict:
        response = await self.post("/api/v1/workflow/modules", payload)
        self.assertEqual(response[0], 201, response[2])
        return json_body(response)

    async def save_definition(self, payload: dict) -> dict:
        response = await self.post("/api/v1/workflow/flows/definitions", payload)
        self.assertEqual(response[0], 201, response[2])
        return json_body(response)

    async def expand(self, definition_id: str, variables: dict | None = None) -> dict:
        response = await self.post(
            f"/api/v1/workflow/flows/definitions/{definition_id}/expand",
            {"variables": variables or {}},
        )
        self.assertEqual(response[0], 200, response[2])
        return json_body(response)

    async def test_module_called_twice_overrides_parameters_and_keeps_ids_unique(self):
        module_id = f"mod_p41_twice_{self.suffix}"
        await self.save_module(
            module(
                module_id,
                [etch_step("etch_module_a", rateMagnitude="${rate}", duration="${duration}")],
                parameters=[
                    {"name": "rate", "default": 0.1, "unit": "um/s"},
                    {"name": "duration", "default": 0.1, "unit": "s"},
                ],
            )
        )
        definition_id = f"def_p41_twice_{self.suffix}"
        flow_id = f"flow_p41_twice_{self.suffix}"
        await self.save_definition(
            definition(
                definition_id,
                flow_id,
                [
                    {
                        "kind": "module-call",
                        "instanceId": "call_first",
                        "moduleId": module_id,
                        "parameters": {"rate": 0.2, "duration": 0.05},
                    },
                    {
                        "kind": "module-call",
                        "instanceId": "call_second",
                        "moduleId": module_id,
                        "parameters": {"rate": 0.4, "duration": 0.2},
                    },
                ],
            )
        )

        expanded = await self.expand(definition_id)
        self.assertEqual(expanded["stepCount"], 2)
        authored = [step["authoredStepId"] for step in expanded["steps"]]
        self.assertEqual(len(authored), len(set(authored)))
        self.assertEqual(
            [step["moduleInstanceIdPath"] for step in expanded["steps"]],
            ["root/call_first", "root/call_second"],
        )
        self.assertEqual(
            [step["step"]["rateMagnitude"] for step in expanded["steps"]], [0.2, 0.4]
        )
        self.assertEqual(
            [step["step"]["duration"] for step in expanded["steps"]], [0.05, 0.2]
        )
        for step in expanded["steps"]:
            self.assertEqual(step["step"]["model"], "isotropic")
            self.assertEqual(step["step"]["operation"], "etch")
            self.assertEqual(step["moduleId"], module_id)

        first, second = expanded["steps"]
        self.assertEqual(
            {key for key in first["step"] if key not in {"id", "rateMagnitude", "duration"}},
            {key for key in second["step"] if key not in {"id", "rateMagnitude", "duration"}},
        )
        self.assertEqual(
            first["parameterProvenance"]["rateMagnitude"]["kind"], "variable"
        )
        self.assertEqual(
            first["parameterProvenance"]["rateMagnitude"]["resolvedValue"], 0.2
        )
        self.assertEqual(
            first["variableRefs"], ["duration", "rate"]
        )

    async def test_nested_material_rate_expressions_keep_provenance(self):
        module_id = f"mod_p41_material_{self.suffix}"
        await self.save_module(
            module(
                module_id,
                [
                    etch_step(
                        "etch_material_a",
                        rateMode="material",
                        materialRates={"Si": "${etch_rate}"},
                    )
                ],
            )
        )
        definition_id = f"def_p41_material_{self.suffix}"
        await self.save_definition(
            definition(
                definition_id,
                f"flow_p41_material_{self.suffix}",
                [
                    {
                        "kind": "module-call",
                        "instanceId": "call_material",
                        "moduleId": module_id,
                    }
                ],
                variables=[{"name": "etch_rate", "default": 0.1, "unit": "um/s"}],
            )
        )

        expanded = await self.expand(definition_id)
        step = expanded["steps"][0]
        self.assertEqual(step["step"]["materialRates"], {"Si": 0.1})
        provenance = step["parameterProvenance"]["materialRates"]
        self.assertEqual(provenance["kind"], "expression")
        self.assertEqual(provenance["resolvedValue"], {"Si": 0.1})
        self.assertEqual(step["variableRefs"], ["etch_rate"])
        self.assertEqual(
            resolve_value(
                {"rates": ["${etch_rate}", {"double": "${etch_rate * 2}"}]},
                {"etch_rate": 0.1},
            ),
            {"rates": [0.1, {"double": 0.2}]},
        )

        response = await self.post(
            f"/api/v1/workflow/flows/definitions/{definition_id}/compile",
            {"project": project()},
        )
        self.assertEqual(response[0], 201, response[2])
        entry = json_body(response)["compile"]["sourceMap"][0]
        self.assertEqual(
            entry["parameterProvenance"]["materialRates"]["kind"], "expression"
        )
        self.assertEqual(
            entry["parameterProvenance"]["materialRates"]["resolvedValue"],
            {"Si": 0.1},
        )
        self.assertEqual(entry["variableRefs"], ["etch_rate"])

    async def test_provenance_compile_identity_keeps_legacy_compile(self):
        module_id = f"mod_p41_identity_{self.suffix}"
        await self.save_module(
            module(
                module_id,
                [etch_step("etch_identity_a", rateMagnitude="${rate}")],
                parameters=[{"name": "rate", "default": 0.1, "unit": "um/s"}],
            )
        )
        definition_id = f"def_p41_identity_{self.suffix}"
        flow_id = f"flow_p41_identity_{self.suffix}"
        await self.save_definition(
            definition(
                definition_id,
                flow_id,
                [
                    {
                        "kind": "module-call",
                        "instanceId": "call_identity",
                        "moduleId": module_id,
                    }
                ],
                variables=[{"name": "rate", "default": 0.1, "unit": "um/s"}],
            )
        )

        store = get_store()
        definition_model = store.get_flow_definition(definition_id)
        expanded = expand_flow_definition(definition_model, store.module_history())
        flow, placements = to_flow_revision(definition_model, expanded)
        flow_paths = {step.authoredStepId: step.flowPath for step in expanded.steps}
        module_revisions = [
            item.model_dump(mode="json") for item in expanded.moduleRevisions
        ]
        variable_set = [
            item.model_dump(mode="json") for item in expanded.variables
        ]
        # This is what the pre-P4-provenance compiler produced for the same
        # definition: same steps and ids, literal-only SourceMap.
        _, binding, legacy = adopt_project(
            project(),
            flow_id=flow_id,
            steps=flow.steps,
            revision=definition_model.revision,
            placements=placements,
            flow_paths=flow_paths,
            variable_set=variable_set,
            module_revisions=module_revisions,
        )
        store.register_binding(binding)
        store.register_compile(legacy)
        legacy_id = legacy.compileRecord.compileId
        legacy_source_map_hash = legacy.compileRecord.sourceMapHash
        self.assertEqual(
            legacy.sourceMap[0].parameterProvenance["rateMagnitude"].kind,
            "literal",
        )

        response = await self.post(
            f"/api/v1/workflow/flows/definitions/{definition_id}/compile",
            {"project": project()},
        )
        self.assertEqual(response[0], 201, response[2])
        body = json_body(response)
        self.assertNotEqual(body["compileId"], legacy_id)
        self.assertNotEqual(
            body["compile"]["compileRecord"]["sourceMapHash"],
            legacy_source_map_hash,
        )
        self.assertEqual(
            body["compile"]["sourceMap"][0]["parameterProvenance"]["rateMagnitude"]["kind"],
            "variable",
        )
        # The old envelope stays addressable under its original id.
        retained = store.get_compile(legacy_id)
        self.assertEqual(retained.compileRecord.compileId, legacy_id)
        self.assertEqual(retained.compileRecord.sourceMapHash, legacy_source_map_hash)

        # P0 callers pass no provenance at all: the flow payload and therefore
        # compileId/sourceMapHash must stay exactly what the legacy path emits.
        plain_flow, plain_binding, plain = adopt_project(
            project(), flow_id=f"flow_p0_identity_{self.suffix}"
        )
        self.assertEqual(
            plain.compileRecord.flowRevisionHash,
            sha256_hex(plain_flow.model_dump(mode="json", exclude_none=True)),
        )
        self.assertEqual(
            plain.compileRecord.sourceMapHash, source_map_hash(plain.sourceMap)
        )
        repeated = compile_flow(plain_flow, plain_binding)
        self.assertEqual(repeated.compileRecord.compileId, plain.compileRecord.compileId)
        self.assertEqual(repeated.compileRecord.sourceMapHash, plain.compileRecord.sourceMapHash)

    async def test_repeat_count_uses_loop_variable_and_index(self):
        module_id = f"mod_p41_count_{self.suffix}"
        await self.save_module(
            module(
                module_id,
                [etch_step("etch_count_a", rateMagnitude="${level * 0.25 + index * 0.5}")],
                parameters=[{"name": "level", "default": 0, "unit": None}],
            )
        )
        definition_id = f"def_p41_count_{self.suffix}"
        await self.save_definition(
            definition(
                definition_id,
                f"flow_p41_count_{self.suffix}",
                [
                    {
                        "kind": "repeat",
                        "repeatId": "loop_level",
                        "loopVariable": "level",
                        "count": 3,
                        "body": [
                            {
                                "kind": "module-call",
                                "instanceId": "call_level",
                                "moduleId": module_id,
                                "parameters": {"level": "${level}"},
                            }
                        ],
                    }
                ],
            )
        )

        expanded = await self.expand(definition_id)
        self.assertEqual(expanded["stepCount"], 3)
        authored = [step["authoredStepId"] for step in expanded["steps"]]
        self.assertEqual(len(authored), len(set(authored)))
        self.assertEqual(
            [step["step"]["rateMagnitude"] for step in expanded["steps"]],
            [0.0, 0.75, 1.5],
        )
        self.assertEqual(
            [iteration["value"] for iteration in expanded["iterations"]], [0, 1, 2]
        )
        self.assertEqual(
            [iteration["index"] for iteration in expanded["iterations"]], [0, 1, 2]
        )
        self.assertEqual(
            [iteration["variable"] for iteration in expanded["iterations"]],
            ["level", "level", "level"],
        )
        self.assertEqual(
            [step["iterationKey"] for step in expanded["steps"]], ["i0", "i1", "i2"]
        )

    async def test_repeat_values_binds_each_value(self):
        definition_id = f"def_p41_values_{self.suffix}"
        await self.save_definition(
            definition(
                definition_id,
                f"flow_p41_values_{self.suffix}",
                [
                    {
                        "kind": "repeat",
                        "repeatId": "loop_rates",
                        "loopVariable": "rate",
                        "values": [0.05, 0.1],
                        "body": [
                            {
                                "kind": "step",
                                "step": etch_step(
                                    "step_repeat_01", rateMagnitude="${rate}"
                                ),
                            }
                        ],
                    }
                ],
            )
        )

        expanded = await self.expand(definition_id)
        self.assertEqual(expanded["stepCount"], 2)
        authored = [step["authoredStepId"] for step in expanded["steps"]]
        self.assertEqual(len(authored), len(set(authored)))
        self.assertEqual(
            [step["step"]["rateMagnitude"] for step in expanded["steps"]], [0.05, 0.1]
        )
        self.assertEqual(
            [iteration["value"] for iteration in expanded["iterations"]], [0.05, 0.1]
        )

    async def test_expression_arithmetic_functions_and_string_interpolation(self):
        definition_id = f"def_p41_expr_{self.suffix}"
        await self.save_definition(
            definition(
                definition_id,
                f"flow_p41_expr_{self.suffix}",
                [
                    {
                        "kind": "step",
                        "step": etch_step(
                            "step_expr_01",
                            name="etch ${a} um",
                            rateMagnitude="${min(a, b) + max(a, b) + abs(-0.25) - round(0.4)}",
                            duration="${(a + b) * 2 - 4 / 2}",
                        ),
                    }
                ],
                variables=[
                    {"name": "a", "default": 1},
                    {"name": "b", "default": 0.4},
                ],
            )
        )

        expanded = await self.expand(definition_id)
        step = expanded["steps"][0]["step"]
        self.assertEqual(step["name"], "etch 1 um")
        self.assertAlmostEqual(step["rateMagnitude"], 1.65)
        self.assertAlmostEqual(step["duration"], 0.8)
        self.assertEqual(evaluate_expression("2 ** 3 - -1", {}), 9)

        overridden = await self.expand(definition_id, {"a": 1.1, "b": 0.5})
        overridden_step = overridden["steps"][0]["step"]
        self.assertEqual(overridden["variables"][0]["source"], "override")
        self.assertEqual(
            overridden_step["rateMagnitude"],
            min(1.1, 0.5) + max(1.1, 0.5) + abs(-0.25) - round(0.4),
        )
        self.assertEqual(overridden_step["name"], "etch 1.1 um")

    async def test_invalid_expression_and_unknown_variable_return_422(self):
        cases = {
            "attribute": "${a.__class__}",
            "call": "${__import__('os').getcwd()}",
            "unknown": "${missing_value + 1}",
            "syntax": "${a +}",
        }
        for label, expression in cases.items():
            with self.subTest(label=label):
                definition_id = f"def_p41_bad_{label}_{self.suffix}"
                await self.save_definition(
                    definition(
                        definition_id,
                        f"flow_p41_bad_{label}_{self.suffix}",
                        [
                            {
                                "kind": "step",
                                "step": etch_step(
                                    "step_bad_expr_01", rateMagnitude=expression
                                ),
                            }
                        ],
                        variables=[{"name": "a", "default": 2}],
                    )
                )
                response = await self.post(
                    f"/api/v1/workflow/flows/definitions/{definition_id}/expand",
                    {"variables": {}},
                )
                self.assertEqual(response[0], 422, response[2])

    async def test_nested_modules_pass_parameters(self):
        inner_id = f"mod_p41_inner_{self.suffix}"
        outer_id = f"mod_p41_outer_{self.suffix}"
        await self.save_module(
            module(
                inner_id,
                [etch_step("etch_inner_a", rateMagnitude="${scale}")],
                parameters=[{"name": "scale", "default": 0.1, "unit": "um/s"}],
            )
        )
        await self.save_module(
            module(
                outer_id,
                [
                    {
                        "id": "call_inner",
                        "moduleId": inner_id,
                        "parameters": {"scale": "${base * 2}"},
                    }
                ],
                parameters=[{"name": "base", "default": 0.05, "unit": "um/s"}],
            )
        )
        definition_id = f"def_p41_nested_{self.suffix}"
        await self.save_definition(
            definition(
                definition_id,
                f"flow_p41_nested_{self.suffix}",
                [
                    {
                        "kind": "module-call",
                        "instanceId": "call_outer",
                        "moduleId": outer_id,
                        "parameters": {"base": 0.05},
                    }
                ],
            )
        )

        expanded = await self.expand(definition_id)
        self.assertEqual(expanded["stepCount"], 1)
        step = expanded["steps"][0]
        self.assertEqual(step["step"]["rateMagnitude"], 0.1)
        self.assertEqual(step["moduleInstanceIdPath"], "root/call_outer/call_inner")
        self.assertEqual(step["step"]["id"], "call_outer__call_inner__etch_inner_a")
        self.assertEqual(
            expanded["moduleInstances"][0]["parameters"], {"base": 0.05}
        )
        self.assertEqual(
            expanded["moduleInstances"][1]["parameters"], {"scale": 0.1}
        )

    async def test_depth_and_step_limits_return_422(self):
        module_ids = [f"mod_p41_depth_{self.suffix}_{index}" for index in range(MAX_EXPANSION_DEPTH + 2)]
        await self.save_module(module(module_ids[0], [etch_step("etch_depth_a")]))
        for index in range(1, len(module_ids)):
            await self.save_module(
                module(
                    module_ids[index],
                    [{"id": "cal", "moduleId": module_ids[index - 1]}],
                )
            )
        definition_id = f"def_p41_depth_{self.suffix}"
        await self.save_definition(
            definition(
                definition_id,
                f"flow_p41_depth_{self.suffix}",
                [
                    {
                        "kind": "module-call",
                        "instanceId": "call_deep",
                        "moduleId": module_ids[-1],
                    }
                ],
            )
        )
        depth_response = await self.post(
            f"/api/v1/workflow/flows/definitions/{definition_id}/expand", {"variables": {}}
        )
        self.assertEqual(depth_response[0], 422, depth_response[2])
        self.assertIn("depth", json_body(depth_response)["detail"])

        wide_id = f"mod_p41_wide_{self.suffix}"
        await self.save_module(
            module(
                wide_id,
                [etch_step(f"step_wide_{index:02d}") for index in range(11)],
            )
        )
        wide_definition_id = f"def_p41_wide_{self.suffix}"
        await self.save_definition(
            definition(
                wide_definition_id,
                f"flow_p41_wide_{self.suffix}",
                [
                    {
                        "kind": "repeat",
                        "repeatId": "loop_wide",
                        "loopVariable": "index_value",
                        "count": 50,
                        "body": [
                            {
                                "kind": "module-call",
                                "instanceId": "call_wide",
                                "moduleId": wide_id,
                            }
                        ],
                    }
                ],
            )
        )
        step_response = await self.post(
            f"/api/v1/workflow/flows/definitions/{wide_definition_id}/expand",
            {"variables": {}},
        )
        self.assertEqual(step_response[0], 422, step_response[2])
        self.assertIn("step limit", json_body(step_response)["detail"])
        self.assertGreater(50 * 11, MAX_EXPANDED_STEPS)

    async def test_compile_is_stable_and_variable_overrides_are_scoped(self):
        module_id = f"mod_p41_compile_{self.suffix}"
        await self.save_module(
            module(
                module_id,
                [
                    etch_step("etch_variable_a", rateMagnitude="${rate}"),
                    etch_step("etch_constant_b", rateMagnitude=0.3),
                ],
                parameters=[{"name": "rate", "default": 0.1, "unit": "um/s"}],
            )
        )
        definition_id = f"def_p41_compile_{self.suffix}"
        await self.save_definition(
            definition(
                definition_id,
                f"flow_p41_compile_{self.suffix}",
                [
                    {
                        "kind": "module-call",
                        "instanceId": "call_scope",
                        "moduleId": module_id,
                        "parameters": {"rate": "${rate}"},
                    }
                ],
                variables=[{"name": "rate", "default": 0.1, "unit": "um/s"}],
            )
        )
        expanded = await self.expand(definition_id)
        expanded_authored = [step["authoredStepId"] for step in expanded["steps"]]
        expanded_paths = [step["flowPath"] for step in expanded["steps"]]

        first = await self.post(
            f"/api/v1/workflow/flows/definitions/{definition_id}/compile",
            {"project": project()},
        )
        self.assertEqual(first[0], 201, first[2])
        first_body = json_body(first)
        self.assertEqual(first_body["stepCount"], 2)
        self.assertEqual(
            [entry["authoredStepId"] for entry in first_body["compile"]["sourceMap"]],
            expanded_authored,
        )
        self.assertEqual(
            [entry["flowPath"] for entry in first_body["compile"]["sourceMap"]],
            expanded_paths,
        )
        self.assertEqual(
            [step["id"] for step in first_body["authoredSteps"]], expanded_authored
        )
        source_map_by_authored = {
            entry["authoredStepId"]: entry
            for entry in first_body["compile"]["sourceMap"]
        }
        variable_entry = source_map_by_authored[expanded_authored[0]]
        self.assertEqual(
            variable_entry["parameterProvenance"]["rateMagnitude"]["kind"], "variable"
        )
        self.assertEqual(
            variable_entry["parameterProvenance"]["rateMagnitude"]["source"], "rate"
        )
        self.assertEqual(variable_entry["parameterProvenance"]["rateMagnitude"]["resolvedValue"], 0.1)
        self.assertEqual(variable_entry["variableRefs"], ["rate"])
        constant_entry = source_map_by_authored[expanded_authored[1]]
        self.assertEqual(
            constant_entry["parameterProvenance"]["rateMagnitude"]["kind"], "literal"
        )
        self.assertEqual(constant_entry["variableRefs"], [])
        first_step_ids = [step["id"] for step in first_body["compile"]["project"]["steps"]]
        self.assertEqual(
            sorted(entry["stepId"] for entry in first_body["compile"]["sourceMap"]),
            sorted(first_step_ids),
        )

        repeated = await self.post(
            f"/api/v1/workflow/flows/definitions/{definition_id}/compile",
            {"project": project()},
        )
        self.assertEqual(repeated[0], 201, repeated[2])
        repeated_body = json_body(repeated)
        self.assertEqual(repeated_body["compileId"], first_body["compileId"])
        self.assertEqual(
            [step["id"] for step in repeated_body["compile"]["project"]["steps"]],
            first_step_ids,
        )

        changed = await self.post(
            f"/api/v1/workflow/flows/definitions/{definition_id}/compile",
            {"project": project(), "variables": {"rate": 0.4}},
        )
        self.assertEqual(changed[0], 201, changed[2])
        changed_body = json_body(changed)
        self.assertNotEqual(changed_body["compileId"], first_body["compileId"])
        self.assertEqual(
            [step["id"] for step in changed_body["compile"]["project"]["steps"]],
            first_step_ids,
        )
        self.assertEqual(
            [entry["authoredStepId"] for entry in changed_body["compile"]["sourceMap"]],
            expanded_authored,
        )
        self.assertEqual(
            [step["rateMagnitude"] for step in changed_body["compile"]["project"]["steps"]],
            [0.4, 0.3],
        )
        self.assertEqual(
            [step["rateMagnitude"] for step in first_body["compile"]["project"]["steps"]],
            [0.1, 0.3],
        )
        self.assertEqual(
            changed_body["compile"]["compileRecord"]["variableSet"][0]["value"], 0.4
        )

    async def test_module_revision_pin_keeps_old_expansion_and_compile(self):
        module_id = f"mod_p41_revision_{self.suffix}"
        await self.save_module(
            module(
                module_id,
                [etch_step("etch_revision_a", rateMagnitude="${rate}", duration=0.1)],
                parameters=[{"name": "rate", "default": 0.1, "unit": "um/s"}],
            )
        )
        flow_id = f"flow_p41_revision_{self.suffix}"
        pinned_id = f"def_p41_pinned_{self.suffix}"
        pinned = definition(
            pinned_id,
            flow_id,
            [
                {
                    "kind": "module-call",
                    "instanceId": "call_revision",
                    "moduleId": module_id,
                    "moduleRevision": 1,
                    "parameters": {"rate": 0.1},
                }
            ],
        )
        await self.save_definition(pinned)

        expanded = await self.expand(pinned_id)
        self.assertEqual(
            expanded["moduleRevisions"], [{"moduleId": module_id, "revision": 1}]
        )
        self.assertEqual(expanded["moduleInstances"][0]["moduleRevision"], 1)
        self.assertEqual(expanded["steps"][0]["moduleRevision"], 1)
        self.assertEqual(expanded["steps"][0]["step"]["duration"], 0.1)

        first = await self.post(
            f"/api/v1/workflow/flows/definitions/{pinned_id}/compile", {"project": project()}
        )
        self.assertEqual(first[0], 201, first[2])
        first_body = json_body(first)
        self.assertEqual(
            first_body["compile"]["compileRecord"]["moduleRevisions"],
            [{"moduleId": module_id, "revision": 1}],
        )
        self.assertEqual(
            [step["duration"] for step in first_body["compile"]["project"]["steps"]], [0.1]
        )

        # Editing the module appends revision 2 but must not rewrite revision 1.
        await self.save_module(
            module(
                module_id,
                [etch_step("etch_revision_a", rateMagnitude="${rate}", duration=0.2)],
                parameters=[{"name": "rate", "default": 0.1, "unit": "um/s"}],
            )
        )
        unpinned_id = f"def_p41_unpinned_{self.suffix}"
        await self.save_definition(
            definition(
                unpinned_id,
                flow_id,
                [
                    {
                        "kind": "module-call",
                        "instanceId": "call_revision",
                        "moduleId": module_id,
                        "parameters": {"rate": 0.1},
                    }
                ],
            )
        )
        latest = await self.expand(unpinned_id)
        self.assertEqual(
            latest["moduleRevisions"], [{"moduleId": module_id, "revision": 2}]
        )
        self.assertEqual(latest["moduleInstances"][0]["moduleRevision"], 2)
        self.assertEqual(latest["steps"][0]["step"]["duration"], 0.2)

        still_pinned = await self.expand(pinned_id)
        self.assertEqual(still_pinned["steps"][0]["step"]["duration"], 0.1)
        repeated = await self.post(
            f"/api/v1/workflow/flows/definitions/{pinned_id}/compile", {"project": project()}
        )
        self.assertEqual(repeated[0], 201, repeated[2])
        self.assertEqual(json_body(repeated)["compileId"], first_body["compileId"])

        missing_pin = definition(
            f"def_p41_missing_pin_{self.suffix}",
            flow_id,
            [
                {
                    "kind": "module-call",
                    "instanceId": "call_missing",
                    "moduleId": module_id,
                    "moduleRevision": 99,
                }
            ],
        )
        response = await self.post("/api/v1/workflow/flows/definitions", missing_pin)
        self.assertEqual(response[0], 422, response[2])
        self.assertIn("not retained", json_body(response)["detail"])

    async def test_expression_resource_limits_are_rejected(self):
        for expression in (
            "(((((((2 ** 64) ** 64) ** 64) ** 64) ** 64) ** 64) ** 64)",
            "2 ** 64 ** 64",
            "9" * 500,
            "1e999",
            "0 ** -1",
        ):
            with self.subTest(expression=expression[:32]):
                with self.assertRaises(ExpressionError):
                    evaluate_expression(expression, {})

        definition_id = f"def_p41_limit_expr_{self.suffix}"
        await self.save_definition(
            definition(
                definition_id,
                f"flow_p41_limit_expr_{self.suffix}",
                [
                    {
                        "kind": "step",
                        "step": etch_step(
                            "step_limit_expr_01",
                            rateMagnitude="${(((((((2 ** 64) ** 64) ** 64) ** 64) ** 64) ** 64) ** 64)}",
                        ),
                    }
                ],
            )
        )
        response = await self.post(
            f"/api/v1/workflow/flows/definitions/{definition_id}/expand", {"variables": {}}
        )
        self.assertEqual(response[0], 422, response[2])

    async def test_compile_uses_definition_solver_and_rejects_flow_id_override(self):
        definition_id = f"def_p41_solver_{self.suffix}"
        flow_id = f"flow_p41_solver_{self.suffix}"
        solver = dict(SOLVER)
        solver["timeStepRatio"] = 0.5
        payload = definition(
            definition_id,
            flow_id,
            [{"kind": "step", "step": etch_step("step_solver_01")}],
        )
        payload["solver"] = solver
        await self.save_definition(payload)

        compiled = await self.post(
            f"/api/v1/workflow/flows/definitions/{definition_id}/compile", {"project": project()}
        )
        self.assertEqual(compiled[0], 201, compiled[2])
        self.assertEqual(
            json_body(compiled)["compile"]["project"]["solver"]["timeStepRatio"], 0.5
        )

        mismatch = await self.post(
            f"/api/v1/workflow/flows/definitions/{definition_id}/compile",
            {"project": project(), "flowId": f"flow_p41_other_{self.suffix}"},
        )
        self.assertEqual(mismatch[0], 422, mismatch[2])
        self.assertIn("flowId", json_body(mismatch)["detail"])

    def test_schema_five_database_backfills_module_revision_history(self):
        module_id = f"mod_p41_legacy_{self.suffix}"
        path = storage.VAR / f"test-p41-schema5-{self.suffix}.sqlite3"
        store = WorkflowStore(path)
        store.initialize()
        store.save_module(
            ModuleV1.model_validate(
                module(module_id, [etch_step("etch_legacy_a")])
            )
        )
        # Reproduce a schema-5 database: only the current module row existed.
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE module_revisions")
            connection.execute("UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'")
        migrated = WorkflowStore(path)
        migrated.initialize()
        self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
        self.assertEqual(
            list(migrated.module_history()[module_id]), [1]
        )
        self.assertEqual(
            migrated.get_module(module_id, revision=1).name,
            f"{module_id} module",
        )


if __name__ == "__main__":
    unittest.main()
