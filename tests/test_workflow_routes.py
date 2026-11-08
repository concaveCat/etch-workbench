"""P0-2 route plans: branch materialization and prefix compatibility."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from backend.workflow import (
    AttemptV1,
    CheckpointRefV1,
    PrefixCompatibilityV1,
    RoutePlanV1,
    RouteV1,
    SegmentReuseV1,
    SegmentV1,
    materialize_attempt,
)


FLOW_DIGEST = "f" * 64
GEOMETRY_HASH = "b" * 64
STEP_DIGEST = "d" * 64
COMPILE_ID = "c_" + "1" * 24


def checkpoint(name: str, step_id: str, hash_char: str) -> CheckpointRefV1:
    return CheckpointRefV1(
        checkpointId=name,
        runId="run_checkpoint_01",
        stepId=step_id,
        stateHash=hash_char * 64,
    )


def segment(
    segment_id: str,
    attempt_id: str,
    *,
    start_after: str | None,
    through: str,
    start_checkpoint: CheckpointRefV1 | None,
    end_checkpoint: CheckpointRefV1 | None,
    status: str = "succeeded",
) -> SegmentV1:
    return SegmentV1(
        segmentId=segment_id,
        attemptId=attempt_id,
        sourceCompileId=COMPILE_ID,
        segmentProjectHash="a" * 64,
        startAfterStepId=start_after,
        throughStepId=through,
        startCheckpointRef=start_checkpoint,
        endCheckpointRef=end_checkpoint,
        runId=f"run_{segment_id}",
        provenanceInputHash="9" * 64,
        flowDigest=FLOW_DIGEST,
        executionHash="e" * 64,
        resultDigest="d" * 64,
        status=status,
    )


def compatibility(*, step_ids: list[str] | None = None, run_seed: int = 1) -> PrefixCompatibilityV1:
    return PrefixCompatibilityV1(
        prefixDigest=FLOW_DIGEST,
        geometryBindingHash=GEOMETRY_HASH,
        solverDigest="5" * 64,
        seedPolicy="shared-run-seed",
        runSeed=run_seed,
        stepIds=step_ids or ["s001", "s002", "s003"],
        resolvedStepDigest=STEP_DIGEST,
    )


def plan_payload() -> dict:
    cp3 = checkpoint("cp_after_s003", "s003", "3")
    cp5 = checkpoint("cp_after_s005", "s005", "5")
    return {
        "route": {
            "routeId": "R01",
            "flowId": "flow_trench_01",
            "name": "branch route",
            "headAttemptId": "A02",
        },
        "stepOrder": ["s001", "s002", "s003", "s004", "s005"],
        "attempts": [
            {
                "attemptId": "A01",
                "routeId": "R01",
                "compileId": COMPILE_ID,
                "submissionKey": "sub_a1",
                "startAfterStepId": None,
                "throughStepId": "s005",
                "seedPolicy": "shared-run-seed",
                "runSeed": 1,
                "flowDigest": FLOW_DIGEST,
                "status": "succeeded",
            },
            {
                "attemptId": "A02",
                "routeId": "R01",
                "parentAttemptId": "A01",
                "compileId": COMPILE_ID,
                "submissionKey": "sub_a2",
                "baseCheckpointRef": cp3.model_dump(mode="json"),
                "startAfterStepId": "s003",
                "throughStepId": "s005",
                "changedStepIds": ["s004"],
                "prefixCompatibility": compatibility().model_dump(mode="json"),
                "seedPolicy": "shared-run-seed",
                "runSeed": 1,
                "flowDigest": FLOW_DIGEST,
                "status": "planned",
            },
        ],
        "segments": [
            segment("S01", "A01", start_after=None, through="s003", start_checkpoint=None, end_checkpoint=cp3).model_dump(mode="json"),
            segment("S02", "A01", start_after="s003", through="s005", start_checkpoint=cp3, end_checkpoint=cp5).model_dump(mode="json"),
            segment("S03", "A02", start_after="s003", through="s005", start_checkpoint=cp3, end_checkpoint=cp5, status="planned").model_dump(mode="json"),
        ],
        "reuse": [
            {
                "reuseId": "RU1",
                "attemptId": "A02",
                "reusedFromSegmentId": "S01",
                "fromStepId": "s001",
                "toStepId": "s003",
                "compatibility": compatibility().model_dump(mode="json"),
            }
        ],
    }


class RoutePlanTests(unittest.TestCase):
    def test_branch_materializes_reused_prefix_then_new_segment(self):
        plan = RoutePlanV1.model_validate(plan_payload())
        refs = materialize_attempt(plan, "A02")
        self.assertEqual([ref.segmentId for ref in refs], ["S01", "S03"])
        self.assertEqual([ref.reused for ref in refs], [True, False])
        self.assertEqual(refs[0].fromStepId, "s001")
        self.assertEqual(refs[0].toStepId, "s003")
        self.assertEqual(refs[1].fromStepId, "s004")
        self.assertEqual(refs[1].toStepId, "s005")

    def test_gap_in_attempt_segments_is_rejected(self):
        payload = plan_payload()
        payload["segments"][2]["startAfterStepId"] = "s004"
        with self.assertRaises(ValidationError):
            RoutePlanV1.model_validate(payload)

    def test_prefix_compatibility_mismatch_is_rejected(self):
        payload = plan_payload()
        payload["reuse"][0]["compatibility"] = compatibility(run_seed=2).model_dump(mode="json")
        with self.assertRaises(ValidationError):
            RoutePlanV1.model_validate(payload)

    def test_self_parent_cycle_is_rejected(self):
        payload = plan_payload()
        payload["attempts"][1]["parentAttemptId"] = "A02"
        with self.assertRaises(ValidationError):
            RoutePlanV1.model_validate(payload)

    def test_checkpoint_discontinuity_is_rejected(self):
        payload = plan_payload()
        payload["segments"][2]["startCheckpointRef"] = checkpoint("wrong_cp", "s005", "9").model_dump(mode="json")
        with self.assertRaises(ValidationError):
            RoutePlanV1.model_validate(payload)

    def test_succeeded_noninitial_segment_requires_start_checkpoint(self):
        payload = plan_payload()
        payload["segments"][1]["startCheckpointRef"] = None
        with self.assertRaises(ValidationError):
            RoutePlanV1.model_validate(payload)

    def test_segment_parent_cycle_is_rejected(self):
        payload = plan_payload()
        payload["segments"][0]["parentSegmentId"] = "S02"
        payload["segments"][1]["parentSegmentId"] = "S01"
        with self.assertRaises(ValidationError):
            RoutePlanV1.model_validate(payload)

    def test_missing_segment_parent_is_rejected(self):
        payload = plan_payload()
        payload["segments"][1]["parentSegmentId"] = "S99"
        with self.assertRaises(ValidationError):
            RoutePlanV1.model_validate(payload)

    def test_start_checkpoint_must_match_start_after_step(self):
        payload = plan_payload()
        payload["segments"][1]["startCheckpointRef"] = checkpoint("wrong_start", "s002", "2").model_dump(mode="json")
        with self.assertRaises(ValidationError):
            RoutePlanV1.model_validate(payload)

    def test_missing_route_head_is_rejected(self):
        payload = plan_payload()
        payload["route"]["headAttemptId"] = "A09"
        with self.assertRaises(ValidationError):
            RoutePlanV1.model_validate(payload)

    def test_reuse_of_non_succeeded_segment_is_rejected(self):
        payload = plan_payload()
        payload["segments"][0]["status"] = "planned"
        with self.assertRaises(ValidationError):
            RoutePlanV1.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
