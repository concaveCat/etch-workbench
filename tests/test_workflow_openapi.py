"""P0-6 typed workflow routes appear in a generated OpenAPI schema."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from backend.main import app
from backend.workflow.api import EquivalenceCheckRequest, MeasureSnapshotRequest
from backend.workflow.equivalence import ComparisonProtocolV1
from backend.workflow.metrology import SnapshotPayloadV1


class WorkflowOpenApiTests(unittest.TestCase):
    def test_workflow_routes_are_in_generated_schema(self):
        schema = app.openapi()
        paths = schema["paths"]
        for path in (
            "/api/v1/workflow/routes",
            "/api/v1/workflow/routes/{route_id}/measurement-decisions",
            "/api/v1/workflow/routes/{route_id}/measurement-decisions/{decision_id}",
            "/api/v1/workflow/routes/{route_id}/measurement-decisions/{decision_id}/replay",
            "/api/v1/workflow/routes/{route_id}/workflow-runs",
            "/api/v1/workflow/routes/{route_id}/workflow-runs/{workflow_run_id}",
            "/api/v1/workflow/compiles",
            "/api/v1/workflow/measurements",
            "/api/v1/workflow/measurements/{measurement_id}/bind",
            "/api/v1/workflow/measurements/{measurement_id}/provenance",
            "/api/v1/workflow/equivalence/check",
            "/api/v1/workflow/gc/dry-run",
            "/api/v1/workflow/modules",
            "/api/v1/workflow/modules/{module_id}",
            "/api/v1/workflow/flows/definitions",
            "/api/v1/workflow/flows/definitions/{definition_id}",
            "/api/v1/workflow/flows/definitions/{definition_id}/expand",
            "/api/v1/workflow/flows/definitions/{definition_id}/compile",
            "/api/v1/workflow/library/export",
            "/api/v1/workflow/library/bundles",
            "/api/v1/workflow/library/bundles/{bundle_id}",
            "/api/v1/workflow/library/bundles/{bundle_id}/import",
            "/api/v1/workflow/library/import",
            "/api/v1/workflow/library/items",
            "/api/v1/workflow/library/items/{item_id}",
            "/api/v1/workflow/projects",
            "/api/v1/workflow/projects/normalize",
            "/api/v1/workflow/projects/{project_id}",
            "/api/v1/workflow/projects/{project_id}/state",
        ):
            self.assertIn(path, paths)
        self.assertIn("RoutePlanV1", schema["components"]["schemas"])
        self.assertIn("MeasurementResultV1", schema["components"]["schemas"])
        for name in (
            "ModuleV1",
            "FlowDefinitionV2-Input",
            "FlowDefinitionV2-Output",
            "ExpandedFlowV1",
            "ExpandFlowDefinitionRequest",
            "LibraryBundleV1-Input",
            "LibraryBundleV1-Output",
            "LibraryImportResultV1",
            "LibraryItemV1",
            "LibraryItemCreateRequest",
            "LibraryItemUpdateRequest",
            "LibraryItemListResponse",
            "ProjectRecordV1",
            "ProjectListResponse",
            "ProjectWorkflowStateV1",
            "ProjectWorkflowSegmentRunV1",
        ):
            self.assertIn(name, schema["components"]["schemas"])
        for name in (
            "MeasurementDecisionV1",
            "MeasurementDecisionRequestV1",
            "MeasurementDecisionResponse",
            "MeasurementDecisionListResponse",
            "WorkflowRunSummaryV1",
            "WorkflowRunListResponse",
            "WorkflowRunDetailV1",
        ):
            self.assertIn(name, schema["components"]["schemas"])
        decision_request = schema["components"]["schemas"]["MeasurementDecisionRequestV1"]
        self.assertFalse(decision_request.get("additionalProperties", False))
        self.assertIn("branches", decision_request["properties"])
        self.assertIn("operator", decision_request["properties"])
        decision = schema["components"]["schemas"]["MeasurementDecisionV1"]
        self.assertIn("resolvedParameters", decision["properties"])
        self.assertIn("replayOf", decision["properties"])
        expand_request = schema["components"]["schemas"]["ExpandFlowDefinitionRequest"]
        self.assertFalse(expand_request.get("additionalProperties", False))
        self.assertIn("variables", expand_request["properties"])
        self.assertNotEqual(expand_request["properties"]["variables"], {})
        project_record = schema["components"]["schemas"]["ProjectRecordV1"]
        for field in ("projectId", "name", "dimension", "stepCount", "updatedAt"):
            self.assertIn(field, project_record["required"])
        self.assertFalse(project_record.get("additionalProperties", False))
        project_state = schema["components"]["schemas"]["ProjectWorkflowStateV1"]
        self.assertFalse(project_state.get("additionalProperties", False))
        for field in (
            "schemaVersion",
            "compileId",
            "compileBasis",
            "routeId",
            "segmentRuns",
            "measurementIds",
            "experimentIds",
            "updatedAt",
        ):
            self.assertIn(field, project_state["properties"])
        self.assertEqual(
            project_state["properties"]["compileBasis"]["anyOf"][0]["maxLength"], 128
        )
        segment_run = schema["components"]["schemas"]["ProjectWorkflowSegmentRunV1"]
        self.assertFalse(segment_run.get("additionalProperties", False))
        for field in ("segmentId", "runId", "measurementId"):
            self.assertIn(field, segment_run["properties"])
        state_path = paths["/api/v1/workflow/projects/{project_id}/state"]
        self.assertIn("get", state_path)
        self.assertIn("put", state_path)
        self.assertIn("delete", state_path)
        self.assertEqual(
            state_path["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProjectWorkflowStateV1",
        )
        self.assertEqual(
            state_path["put"]["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProjectWorkflowStateV1",
        )
        self.assertEqual(
            paths["/api/v1/workflow/projects"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProjectV7",
        )
        normalize_path = paths["/api/v1/workflow/projects/normalize"]
        self.assertEqual(
            normalize_path["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProjectV7",
        )
        self.assertEqual(
            normalize_path["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProjectNormalizeResponse",
        )
        self.assertIn("ProjectNormalizeResponse", schema["components"]["schemas"])
        self.assertEqual(
            schema["components"]["schemas"]["ProjectNormalizeResponse"]["properties"]["project"]["$ref"],
            "#/components/schemas/ProjectV7",
        )
        self.assertEqual(
            paths["/api/v1/workflow/projects/{project_id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProjectV7",
        )
        static = json.loads((Path(__file__).resolve().parents[1] / "docs" / "workflow-openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(static["info"]["title"], schema["info"]["title"])
        self.assertEqual(set(static["paths"]), set(paths))
        self.assertEqual(static["components"]["schemas"], schema["components"]["schemas"])
        snapshot = schema["components"]["schemas"]["SnapshotPayloadV1"]
        self.assertFalse(snapshot.get("additionalProperties", False))
        self.assertIn("gridDelta", snapshot["required"])
        self.assertIn("dimension", snapshot["required"])
        self.assertEqual(
            schema["components"]["schemas"]["MeasureSnapshotRequest"]["properties"]["snapshot"]["$ref"],
            "#/components/schemas/SnapshotPayloadV1",
        )
        self.assertEqual(
            schema["components"]["schemas"]["EquivalenceCheckRequest"]["properties"]["left"]["$ref"],
            "#/components/schemas/SnapshotPayloadV1",
        )
        fixture = json.loads((Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "2d-plane-etch.json").read_text(encoding="utf-8"))
        snapshot = SnapshotPayloadV1.model_validate(fixture)
        MeasureSnapshotRequest(recipeId="r", recipeVersion=1, snapshot=snapshot)
        EquivalenceCheckRequest(
            protocol=ComparisonProtocolV1(schemaVersion=1, protocolId="proto", version=1, pointTolerance=0.1),
            left=snapshot,
            right=snapshot,
        )


if __name__ == "__main__":
    unittest.main()
