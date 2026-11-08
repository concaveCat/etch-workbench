"""P0 workflow contract: deterministic compile, stable ids, geometry binding."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from backend.models import (
    AdvectionSettingsV7,
    Domain,
    Domain3D,
    FixedRandom,
    RayTracingSettingsV7,
    SolverV7,
    Units,
)
from backend.workflow import (
    CompiledWorkflowV1,
    FlowRevisionV1,
    GeometryBindingV1,
    compile_flow,
    stable_step_id,
)
from backend.workflow.hashing import full_project_hash


UNITS = Units(length="um", time="s", rate="um/s", angle="deg")


def default_solver(
    *,
    spatial_scheme: str = "ENGQUIST_OSHER_1ST_ORDER",
    smoothing_neighbors: int = 1,
) -> SolverV7:
    return SolverV7(
        timeStepRatio=0.25,
        raysPerPoint=32,
        cpuThreads=1,
        random=FixedRandom(mode="fixed", seed=314159),
        enginePreference="cpu",
        timeoutSeconds=60,
        advection=AdvectionSettingsV7(spatialScheme=spatial_scheme),
        rayTracing=RayTracingSettingsV7(smoothingNeighbors=smoothing_neighbors),
    )


def binding(*, binding_id: str = "bind_trench_01", grid_delta: float = 0.2) -> GeometryBindingV1:
    return GeometryBindingV1(
        schemaVersion=1,
        bindingId=binding_id,
        name="3D trench binding",
        dimension=3,
        units=UNITS,
        domain=Domain3D(
            gridDelta=grid_delta,
            xExtent=4.0,
            yExtent=4.0,
            boundaryXY="reflective",
        ),
        geometry={
            "kind": "trench",
            "width": 1.0,
            "depth": 1.2,
            "taperAngle": 0.0,
            "maskHeight": 0.0,
            "maskTaperAngle": 0.0,
            "substrateMaterial": "Si",
            "maskMaterial": "Mask",
        },
        materialCatalogVersion="4.6.2",
        defaultValuesVersion="1",
    )


def step(
    step_id: str,
    *,
    duration: float = 0.4,
    operation: str = "etch",
    rate: float = 0.2,
) -> dict:
    payload = {
        "id": step_id,
        "name": step_id,
        "enabled": True,
        "operation": operation,
        "model": "isotropic",
        "rateMagnitude": rate,
        "duration": duration,
        "rateMode": "uniform",
    }
    if operation == "deposit":
        payload["depositMaterial"] = "SiO2"
    return payload


def flow(
    *steps: dict,
    flow_id: str = "flow_trench_01",
    revision: int = 1,
    solver_settings: SolverV7 | None = None,
) -> FlowRevisionV1:
    return FlowRevisionV1(
        schemaVersion=1,
        flowId=flow_id,
        revision=revision,
        name="P0 trench flow",
        steps=list(steps) or [step("step_etch_01")],
        solver=solver_settings or default_solver(),
    )


class WorkflowCompileTests(unittest.TestCase):
    def test_stable_step_id_is_structural_not_content_addressed(self):
        first = stable_step_id(
            flow_id="flow_trench_01",
            module_instance_id_path="root",
            authored_step_id="step_etch_01",
            iteration_key="i0",
        )
        second = stable_step_id(
            flow_id="flow_trench_01",
            module_instance_id_path="root",
            authored_step_id="step_etch_01",
            iteration_key="i0",
        )
        other_iteration = stable_step_id(
            flow_id="flow_trench_01",
            module_instance_id_path="root",
            authored_step_id="step_etch_01",
            iteration_key="i1",
        )
        other_flow = stable_step_id(
            flow_id="flow_trench_02",
            module_instance_id_path="root",
            authored_step_id="step_etch_01",
            iteration_key="i0",
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, other_iteration)
        self.assertNotEqual(first, other_flow)
        self.assertRegex(first, r"^s_[a-z2-7]{20}$")

    def test_compile_is_deterministic(self):
        first = compile_flow(flow(), binding())
        second = compile_flow(flow(), binding())

        self.assertEqual(first.compileRecord.compileId, second.compileRecord.compileId)
        self.assertEqual(
            first.project.model_dump(mode="json", exclude_none=True),
            second.project.model_dump(mode="json", exclude_none=True),
        )
        self.assertEqual(
            [entry.model_dump(mode="json") for entry in first.sourceMap],
            [entry.model_dump(mode="json") for entry in second.sourceMap],
        )
        self.assertEqual(first.compileRecord.fullProjectHash, second.compileRecord.fullProjectHash)
        self.assertEqual(first.compileRecord.sourceMapHash, second.compileRecord.sourceMapHash)

    def test_compiled_result_is_isolated_from_input_mutation(self):
        flow_input = flow(step("step_etch_01"))
        binding_input = binding()
        compiled = compile_flow(flow_input, binding_input)
        before_project = compiled.project.model_dump(mode="json", exclude_none=True)
        before_hash = compiled.compileRecord.fullProjectHash

        binding_input.domain.gridDelta = 0.1
        flow_input.solver.timeStepRatio = 0.1
        flow_input.steps[0].duration = 0.9

        self.assertEqual(
            compiled.project.model_dump(mode="json", exclude_none=True),
            before_project,
        )
        self.assertEqual(compiled.compileRecord.fullProjectHash, before_hash)
        self.assertEqual(full_project_hash(compiled.project), before_hash)

    def test_live_envelope_verification_catches_output_mutation(self):
        compiled = compile_flow(flow(step("step_etch_01")), binding())
        compiled.verify()
        compiled.project.steps[0].duration = 0.9
        with self.assertRaises(ValidationError):
            compiled.verify()

    def test_tampered_compiled_project_is_rejected(self):
        compiled = compile_flow(flow(step("step_etch_01")), binding())
        payload = compiled.model_dump(mode="json", exclude_none=True)
        payload["project"]["steps"][0]["duration"] = 0.9
        with self.assertRaises(ValidationError):
            CompiledWorkflowV1.model_validate(payload)

    def test_tampered_source_map_is_rejected(self):
        compiled = compile_flow(flow(step("step_etch_01")), binding())
        payload = compiled.model_dump(mode="json", exclude_none=True)
        payload["sourceMap"][0]["stepId"] = "s_aaaaaaaaaaaaaaaaaaaa"
        with self.assertRaises(ValidationError):
            CompiledWorkflowV1.model_validate(payload)

    def test_tampered_compile_record_is_rejected(self):
        compiled = compile_flow(flow(step("step_etch_01")), binding())
        payload = compiled.model_dump(mode="json", exclude_none=True)
        payload["compileRecord"]["fullProjectHash"] = "0" * 64
        with self.assertRaises(ValidationError):
            CompiledWorkflowV1.model_validate(payload)

    def test_parameter_edit_keeps_step_ids_but_changes_project_hash(self):
        before = compile_flow(flow(step("step_etch_01", duration=0.4)), binding())
        after = compile_flow(flow(step("step_etch_01", duration=0.7)), binding())

        self.assertEqual(
            [entry.stepId for entry in before.sourceMap],
            [entry.stepId for entry in after.sourceMap],
        )
        self.assertEqual(
            [entry.authoredStepId for entry in before.sourceMap],
            [entry.authoredStepId for entry in after.sourceMap],
        )
        self.assertNotEqual(
            before.compileRecord.fullProjectHash,
            after.compileRecord.fullProjectHash,
        )

    def test_inserting_a_step_does_not_change_existing_ids(self):
        before = compile_flow(flow(step("step_etch_01"), step("step_dep_01", operation="deposit")), binding())
        after = compile_flow(
            flow(
                step("step_etch_01"),
                step("step_etch_02", duration=0.1),
                step("step_dep_01", operation="deposit"),
            ),
            binding(),
        )

        before_by_authored = {entry.authoredStepId: entry.stepId for entry in before.sourceMap}
        after_by_authored = {entry.authoredStepId: entry.stepId for entry in after.sourceMap}
        self.assertEqual(before_by_authored["step_etch_01"], after_by_authored["step_etch_01"])
        self.assertEqual(before_by_authored["step_dep_01"], after_by_authored["step_dep_01"])
        self.assertNotIn("step_etch_02", before_by_authored)

    def test_geometry_binding_change_changes_binding_and_project_hashes(self):
        first = compile_flow(flow(), binding(grid_delta=0.2))
        second = compile_flow(flow(), binding(grid_delta=0.1))

        self.assertNotEqual(
            first.compileRecord.geometryBindingHash,
            second.compileRecord.geometryBindingHash,
        )
        self.assertNotEqual(
            first.compileRecord.fullProjectHash,
            second.compileRecord.fullProjectHash,
        )
        # Step identity is structural; a geometry change must not renumber it.
        self.assertEqual(
            [entry.stepId for entry in first.sourceMap],
            [entry.stepId for entry in second.sourceMap],
        )

    def test_source_map_records_flow_path_iteration_and_resolved_values(self):
        compiled = compile_flow(flow(step("step_etch_01", duration=0.4)), binding())
        entry = compiled.sourceMap[0]
        self.assertEqual(entry.flowPath, "root/step_etch_01")
        self.assertEqual(entry.moduleInstanceIdPath, "root")
        self.assertEqual(entry.iterationKey, "i0")
        self.assertEqual(entry.authoredStepId, "step_etch_01")
        self.assertEqual(entry.parameterProvenance["duration"].kind, "literal")
        self.assertEqual(entry.parameterProvenance["duration"].resolvedValue, 0.4)

        edited = compile_flow(flow(step("step_etch_01", duration=0.7)), binding())
        self.assertEqual(edited.sourceMap[0].stepId, entry.stepId)
        self.assertEqual(
            edited.sourceMap[0].parameterProvenance["duration"].resolvedValue,
            0.7,
        )

    def test_2d_binding_compiles_to_project_v7_without_losing_solver_settings(self):
        binding_2d = GeometryBindingV1(
            schemaVersion=1,
            bindingId="bind_plane_2d_01",
            name="2D plane binding",
            dimension=2,
            units=UNITS,
            domain=Domain(gridDelta=0.2, xExtent=4.0, xBoundary="reflective"),
            geometry={"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        )
        compiled = compile_flow(
            flow(
                step("step_etch_01"),
                solver_settings=default_solver(
                    spatial_scheme="WENO_5TH_ORDER",
                    smoothing_neighbors=7,
                ),
            ),
            binding_2d,
        )
        self.assertEqual(compiled.project.schemaVersion, 7)
        self.assertEqual(compiled.project.dimension, 2)
        self.assertEqual(compiled.project.solver.advection.spatialScheme, "WENO_5TH_ORDER")
        self.assertEqual(compiled.project.solver.rayTracing.smoothingNeighbors, 7)

    def test_2d_fin_compiles_to_v7(self):
        binding_2d = GeometryBindingV1(
            schemaVersion=1,
            bindingId="bind_fin_2d_01",
            name="2D fin binding",
            dimension=2,
            units=UNITS,
            domain=Domain(gridDelta=0.2, xExtent=4.0, xBoundary="reflective"),
            geometry={
                "kind": "fin",
                "width": 0.8,
                "height": 1.2,
                "taperAngle": 0.0,
                "maskHeight": 0.0,
                "maskTaperAngle": 0.0,
                "substrateMaterial": "Si",
                "maskMaterial": "Mask",
                "construction": "boolean",
            },
        )
        compiled = compile_flow(flow(step("step_etch_01")), binding_2d)
        self.assertEqual(compiled.project.schemaVersion, 7)
        self.assertEqual(compiled.project.geometry["kind"], "fin")

    def test_2d_binding_requires_2d_domain(self):
        with self.assertRaises(ValidationError):
            GeometryBindingV1(
                schemaVersion=1,
                bindingId="bind_bad_2d_01",
                name="bad 2D binding",
                dimension=2,
                units=UNITS,
                domain=Domain3D(
                    gridDelta=0.2,
                    xExtent=4.0,
                    yExtent=4.0,
                    boundaryXY="reflective",
                ),
                geometry={"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
            )

    def test_2d_binding_requires_2d_domain_type(self):
        with self.assertRaises(ValidationError):
            GeometryBindingV1(
                schemaVersion=1,
                bindingId="bind_bad_3d_01",
                name="bad 3D binding",
                dimension=3,
                units=UNITS,
                domain=Domain(gridDelta=0.2, xExtent=4.0, xBoundary="reflective"),
                geometry={"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
            )


if __name__ == "__main__":
    unittest.main()
