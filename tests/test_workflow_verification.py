"""P0-2 compile-derived prefix compatibility checks."""

from __future__ import annotations

import unittest

from backend.workflow.verification import verify_route_plan_compiles
from tests.workflow_fixtures import compiled_base, compiled_branch, compiled_with_solver, plan_from_compiled


class PrefixVerificationTests(unittest.TestCase):
    def setUp(self):
        self.base = compiled_base()
        self.branch = compiled_branch()
        self.plan = plan_from_compiled(self.base, self.branch, suffix="V1")
        self.compiles = {
            self.base.compileRecord.compileId: self.base,
            self.branch.compileRecord.compileId: self.branch,
        }

    def test_valid_plan_passes(self):
        verify_route_plan_compiles(self.plan, self.compiles)

    def test_changed_run_seed_breaks_prefix_compatibility(self):
        plan = self.plan.model_copy(deep=True)
        plan.attempts[1].runSeed = 999
        with self.assertRaises(ValueError):
            verify_route_plan_compiles(plan, self.compiles)

    def test_tampered_target_prefix_is_rejected(self):
        plan = self.plan.model_copy(deep=True)
        compatibility = plan.attempts[1].prefixCompatibility
        self.assertIsNotNone(compatibility)
        compatibility.resolvedStepDigest = "0" * 64
        with self.assertRaises(ValueError):
            verify_route_plan_compiles(plan, self.compiles)

    def test_tampered_source_reuse_is_rejected(self):
        plan = self.plan.model_copy(deep=True)
        plan.reuse[0].compatibility.resolvedStepDigest = "0" * 64
        with self.assertRaises(ValueError):
            verify_route_plan_compiles(plan, self.compiles)

    def test_missing_source_compile_is_rejected(self):
        with self.assertRaises(ValueError):
            verify_route_plan_compiles(self.plan, {self.branch.compileRecord.compileId: self.branch})

    def test_solver_settings_break_prefix_compatibility(self):
        changed = compiled_with_solver()
        plan = plan_from_compiled(self.base, self.base, suffix="V2")
        plan.attempts[1].compileId = changed.compileRecord.compileId
        plan.attempts[1].flowDigest = changed.compileRecord.fullProjectHash
        plan.segments[2].sourceCompileId = changed.compileRecord.compileId
        plan.segments[2].flowDigest = changed.compileRecord.fullProjectHash
        compiles = {
            self.base.compileRecord.compileId: self.base,
            changed.compileRecord.compileId: changed,
        }
        with self.assertRaises(ValueError):
            verify_route_plan_compiles(plan, compiles)

    def test_segment_compile_digest_mismatch_is_rejected(self):
        plan = self.plan.model_copy(deep=True)
        plan.segments[2].flowDigest = "0" * 64
        with self.assertRaises(ValueError):
            verify_route_plan_compiles(plan, self.compiles)


if __name__ == "__main__":
    unittest.main()
