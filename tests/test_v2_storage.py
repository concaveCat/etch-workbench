"""Tests for the v2 state store and output contract schema.

These tests only touch temporary directories: no server, no worker, and no
workbench data directory.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np

from process_models.v2 import storage
from process_models.v2.output_schema import output_contract_schema
from process_models.v2.state import (
    Artifact,
    CellSetState,
    Field,
    GridSpec,
    Layer,
    LayerState,
    LevelSetState,
    MaterialState,
    MetrologyRecord,
    OutputSet,
    ProcessState,
    VoxelState,
)
from process_models.v2.storage import (
    OUTPUTS_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    StateStore,
    StateStoreError,
)

GRID = GridSpec(dimension=2, origin=(-1.0, -1.0), spacing=0.1, shape=(4, 5))


def _materials() -> MaterialState:
    return MaterialState(order=(10, 30), names={10: "Si", 30: "SiO2"})


def _layers() -> LayerState:
    return LayerState(
        (
            Layer(
                layer_id="gate",
                material_id=30,
                name="SiO2",
                source_step="deposit",
                thickness=0.05,
                metadata={"role": "gate"},
            ),
        )
    )


def _cell_set() -> CellSetState:
    scalar = Field(
        name="P_total",
        unit="1/cm3",
        association="cell",
        dtype="float64",
        shape=(4, 5),
        data=np.arange(20, dtype=np.float64).reshape(4, 5),
    )
    vector = Field(
        name="stress",
        unit="MPa",
        association="cell",
        dtype="float32",
        shape=(4, 5),
        components=3,
        data=np.arange(60, dtype=np.float32).reshape(3, 4, 5),
    )
    material_ids = np.arange(20, dtype=np.int32).reshape(4, 5) % 3
    return CellSetState(
        grid=GRID,
        material_ids=material_ids,
        fields={"P_total": scalar, "stress": vector},
        provenance={"producer": "test"},
    )


def _state() -> ProcessState:
    return ProcessState(materials=_materials(), layers=_layers(), fields=_cell_set())


class _PlanStub:
    """Stand-in for ``ExecutionPlan``; ``save_plan`` only calls ``identity_json``."""

    def __init__(self, identity: dict[str, str]) -> None:
        self._identity = identity

    def identity_json(self) -> dict[str, str]:
        return dict(self._identity)


class StateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.store = StateStore(self.root)

    def _read_json(self, *parts: str) -> Any:
        return json.loads(self.root.joinpath(*parts).read_text(encoding="utf-8"))

    def test_root_and_step_dir_layout(self) -> None:
        self.assertEqual(self.store.root, self.root)
        self.assertEqual(self.store.step_dir("s1"), self.root / "steps" / "s1")
        with self.assertRaises(StateStoreError):
            self.store.step_dir("../escape")

    def test_state_round_trip_with_scalar_and_vector_fields(self) -> None:
        state = _state()
        payload = self.store.save_state("deposit", state)

        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["stepId"], "deposit")
        self.assertEqual(payload["materials"], {"order": [10, 30], "names": {"10": "Si", "30": "SiO2"}})
        self.assertIsNone(payload["geometry"])
        self.assertEqual(payload["layers"]["layers"][0]["layerId"], "gate")
        self.assertEqual(payload["fields"]["materialIds"], {"dtype": "int32", "shape": [4, 5]})
        self.assertEqual(list(payload["fields"]["fields"]), ["P_total", "stress"])

        step = self.store.step_dir("deposit")
        self.assertEqual(self._read_json("steps", "deposit", "state.json"), payload)
        with np.load(step / "fields.npz", allow_pickle=False) as archive:
            self.assertEqual(archive.files, ["field_000", "field_001", "material_ids"])

        restored = self.store.load_state("deposit")
        self.assertEqual(restored.materials, state.materials)
        self.assertEqual(restored.layers, state.layers)
        self.assertIsNone(restored.geometry)
        self.assertIsNone(restored.voxels)

        self.assertIsInstance(restored.fields, CellSetState)
        assert restored.fields is not None
        self.assertEqual(restored.fields.grid, GRID)
        self.assertEqual(restored.fields.provenance, {"producer": "test"})
        self.assertEqual(restored.fields.material_ids.dtype, np.int32)
        np.testing.assert_array_equal(restored.fields.material_ids, state.fields.material_ids)
        self.assertEqual(set(restored.fields.fields), {"P_total", "stress"})
        for name, original in state.fields.fields.items():
            loaded = restored.fields.fields[name]
            self.assertEqual(
                (loaded.unit, loaded.association, loaded.dtype), (original.unit, original.association, original.dtype)
            )
            self.assertEqual(loaded.shape, original.shape)
            self.assertEqual(loaded.components, original.components)
            np.testing.assert_array_equal(loaded.data, original.data)

        scalar = restored.fields.fields["P_total"]
        self.assertEqual(scalar.data.shape, (4, 5))
        vector = restored.fields.fields["stress"]
        self.assertEqual(vector.data.shape, (3, 4, 5))

    def test_omitted_fields_keep_invalidation_metadata_without_arrays(self) -> None:
        state = ProcessState(materials=_materials(), fields=_cell_set())
        payload = self.store.save_state("geometry", state, persist_fields=False)

        self.assertTrue(payload["fieldsOmitted"])
        self.assertIsNotNone(payload["fields"])
        self.assertTrue(payload["fields"]["arraysOmitted"])
        self.assertFalse((self.store.step_dir("geometry") / "fields.npz").exists())

        restored = self.store.load_state("geometry")
        self.assertIsNotNone(restored.fields)
        assert restored.fields is not None
        self.assertIsNone(restored.fields.handle)
        self.assertIsNone(restored.fields.material_ids)
        self.assertTrue(restored.fields.provenance["arraysOmitted"])
        self.assertIsNone(restored.fields.fields["P_total"].data)
        self.assertEqual(restored.fields.grid, GRID)

    def test_state_round_trip_without_fields_skips_npz(self) -> None:
        state = ProcessState(materials=_materials(), layers=_layers())
        payload = self.store.save_state("plain", state)
        self.assertIsNone(payload["fields"])
        self.assertFalse((self.store.step_dir("plain") / "fields.npz").exists())
        self.assertEqual(self.store.load_state("plain"), state)

    def test_outputs_round_trip_with_metrology_and_artifacts(self) -> None:
        outputs = OutputSet(
            metrology=(
                MetrologyRecord(metric="thickness", value=0.05, unit="um", region="center", uncertainty=0.001),
                MetrologyRecord(metric="rate", value=1.25, unit="nm/s", method="analytic"),
            ),
            artifacts=(
                Artifact(kind="vtu", name="surface", path="surface.vtu", metadata={"timestep": 1}),
                Artifact(kind="json", name="summary", metadata={"tags": ["a", "b"]}),
            ),
        )
        payload = self.store.save_outputs("deposit", outputs)

        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["metrology"][0]["metric"], "thickness")
        self.assertEqual(payload["metrology"][1]["method"], "analytic")
        self.assertEqual(payload["artifacts"][0]["kind"], "vtu")
        self.assertEqual(payload["artifacts"][1]["metadata"], {"tags": ["a", "b"]})
        self.assertEqual(self._read_json("steps", "deposit", "outputs.json"), payload)
        self.assertEqual(self.store.load_outputs("deposit"), outputs)

    def test_geometry_ref_round_trip_with_loader(self) -> None:
        handle = object()
        state = ProcessState(
            geometry=LevelSetState(handle=object(), dimension=2, grid_delta=0.2),
            materials=_materials(),
            layers=_layers(),
        )
        payload = self.store.save_state("etch", state, geometry_ref="geometry.vpsd")
        self.assertEqual(
            payload["geometry"],
            {"ref": "geometry.vpsd", "dimension": 2, "gridDelta": 0.2},
        )
        self.assertNotIn("materialOrder", payload["geometry"])
        self.assertEqual(payload["materials"], {"order": [10, 30], "names": {"10": "Si", "30": "SiO2"}})
        (self.store.step_dir("etch") / "geometry.vpsd").write_bytes(b"native-domain")

        references: list[str] = []

        def loader(reference: str) -> Any:
            references.append(reference)
            return handle

        restored = self.store.load_state("etch", geometry_loader=loader)
        self.assertEqual(references, ["geometry.vpsd"])
        assert restored.geometry is not None
        self.assertIs(restored.geometry.handle, handle)
        self.assertEqual(restored.geometry, LevelSetState(handle=handle, dimension=2, grid_delta=0.2))
        self.assertFalse(hasattr(restored.geometry, "material_order"))
        self.assertEqual(restored.materials, state.materials)
        self.assertEqual(restored.layers, state.layers)

    def test_geometry_ref_without_loader_raises(self) -> None:
        state = ProcessState(
            geometry=LevelSetState(handle=object(), dimension=2, grid_delta=0.2),
            materials=_materials(),
        )
        self.store.save_state("etch", state, geometry_ref="geometry.vpsd")
        with self.assertRaises(StateStoreError) as raised:
            self.store.load_state("etch")
        self.assertIn("geometry_loader", str(raised.exception))

    def test_geometry_without_ref_is_not_reconstructed(self) -> None:
        state = ProcessState(geometry=LevelSetState(handle=object(), dimension=2, grid_delta=0.2))
        payload = self.store.save_state("etch", state)
        self.assertEqual(payload["geometry"]["ref"], None)
        self.assertIsNone(self.store.load_state("etch").geometry)

    def test_opaque_field_handle_raises(self) -> None:
        opaque_fields = (
            Field(name="stress", unit="MPa", association="cell", dtype="float32", shape=(4, 5)),
            Field(name="stress", unit="MPa", association="cell", dtype="float32", shape=(4, 5), data=object()),
        )
        for field in opaque_fields:
            with self.subTest(data=type(field.data).__name__):
                state = ProcessState(fields=CellSetState(grid=GRID, material_ids=None, fields={"stress": field}))
                with self.assertRaises(StateStoreError) as raised:
                    self.store.save_state("fields", state)
                self.assertIn("numpy array", str(raised.exception))
                self.assertFalse((self.store.step_dir("fields") / "state.json").exists())

    def test_non_serializable_metadata_raises(self) -> None:
        state = ProcessState(
            layers=LayerState((Layer(layer_id="l", material_id=10, name="Si", metadata={"cb": object()}),))
        )
        with self.assertRaises(StateStoreError):
            self.store.save_state("bad", state)
        self.assertFalse((self.store.step_dir("bad") / "state.json").exists())

        outputs = OutputSet(artifacts=(Artifact(kind="json", name="summary", metadata={"cb": object()}),))
        with self.assertRaises(StateStoreError):
            self.store.save_outputs("bad", outputs)
        self.assertFalse((self.store.step_dir("bad") / "outputs.json").exists())

        with self.assertRaises(StateStoreError):
            self.store.save_flow({"steps": [object()]})
        self.assertFalse((self.root / "flow.json").exists())

    def test_non_finite_metadata_raises(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                state = ProcessState(
                    layers=LayerState((Layer(layer_id="l", material_id=10, name="Si", metadata={"rate": value}),))
                )
                with self.assertRaises(StateStoreError):
                    self.store.save_state("bad", state)

    def test_plan_identity_round_trip(self) -> None:
        identity = {
            "processId": "isotropic",
            "processVersion": "1.0.0",
            "variantId": "native",
            "variantVersion": "1.0.0",
            "implementationHash": "sha256:abc",
            "engineId": "viennaps",
            "engineVersion": "4.7.0",
            "engineFingerprint": "viennaps@4.7.0",
            "adapter": "default",
        }
        returned = self.store.save_plan("deposit", _PlanStub(identity))
        self.assertEqual(returned, identity)
        self.assertEqual(self._read_json("steps", "deposit", "plan.json"), identity)

    def test_flow_json_round_trip(self) -> None:
        flow = {
            "schemaVersion": 1,
            "runDir": str(self.root),
            "steps": [{"process_id": "isotropic", "variant_id": "native"}],
            "versions": {"viennaps": "4.7.0"},
        }
        self.store.save_flow(flow)
        self.assertTrue((self.root / "flow.json").is_file())
        self.assertEqual(self._read_json("flow.json"), flow)

    def test_json_writes_are_atomic(self) -> None:
        state = _state()
        self.store.save_state("s1", state)
        path = self.store.step_dir("s1") / "state.json"
        before = path.read_text(encoding="utf-8")

        broken = ProcessState(
            layers=LayerState((Layer(layer_id="l", material_id=10, name="Si", metadata={"cb": object()}),))
        )
        with self.assertRaises(StateStoreError):
            self.store.save_state("s1", broken)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.store.load_state("s1").layers, state.layers)

        self.store.save_flow({"schemaVersion": 1})
        flow_before = (self.root / "flow.json").read_text(encoding="utf-8")
        with self.assertRaises(StateStoreError):
            self.store.save_flow({"schemaVersion": float("nan")})
        self.assertEqual((self.root / "flow.json").read_text(encoding="utf-8"), flow_before)

        self.assertEqual([entry for entry in self.root.rglob("*.tmp")], [])

    def test_voxels_are_not_persisted(self) -> None:
        state = ProcessState(materials=_materials(), voxels=VoxelState(grid=object()))
        payload = self.store.save_state("vox", state)
        self.assertNotIn("voxels", payload)
        self.assertIsNone(self.store.load_state("vox").voxels)

    def test_unsupported_state_schema_version_raises(self) -> None:
        directory = self.store.step_dir("bad")
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(json.dumps({"schemaVersion": 2}), encoding="utf-8")
        with self.assertRaises(StateStoreError) as raised:
            self.store.load_state("bad")
        self.assertIn("schemaVersion", str(raised.exception))

    def test_state_and_outputs_schema_versions_are_independent(self) -> None:
        outputs = OutputSet(
            metrology=(MetrologyRecord(metric="thickness", value=0.05, unit="um"),),
            artifacts=(Artifact(kind="json", name="summary", metadata={"tags": ["a"]}),),
        )
        self.assertEqual(STATE_SCHEMA_VERSION, 1)
        self.assertEqual(OUTPUTS_SCHEMA_VERSION, 1)

        # Bumping the state schema must not touch what outputs.json writes.
        with mock.patch.object(storage, "STATE_SCHEMA_VERSION", 7):
            state_payload = self.store.save_state("coupled", _state())
            outputs_payload = self.store.save_outputs("coupled", outputs)

            self.assertEqual(state_payload["schemaVersion"], 7)
            self.assertEqual(outputs_payload["schemaVersion"], OUTPUTS_SCHEMA_VERSION)
            self.assertEqual(
                self._read_json("steps", "coupled", "outputs.json")["schemaVersion"],
                OUTPUTS_SCHEMA_VERSION,
            )
            self.assertIsNotNone(self.store.load_state("coupled"))
            self.assertEqual(self.store.load_outputs("coupled"), outputs)

        # And bumping the outputs schema must not touch state.json.
        with mock.patch.object(storage, "OUTPUTS_SCHEMA_VERSION", 2):
            outputs_payload = self.store.save_outputs("decoupled", outputs)
            state_payload = self.store.save_state("decoupled", _state())

            self.assertEqual(outputs_payload["schemaVersion"], 2)
            self.assertEqual(state_payload["schemaVersion"], STATE_SCHEMA_VERSION)
            restored = self.store.load_outputs("decoupled")
            self.assertEqual(restored.schema_version, 2)
            self.assertEqual(restored.metrology, outputs.metrology)
            self.assertEqual(restored.artifacts, outputs.artifacts)

    def test_legacy_outputs_schema_v1_file_stays_readable(self) -> None:
        directory = self.store.step_dir("legacy")
        directory.mkdir(parents=True)
        (directory / "outputs.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "metrology": [
                        {
                            "metric": "thickness",
                            "value": 0.05,
                            "unit": "um",
                            "region": None,
                            "method": "plugin",
                            "uncertainty": None,
                        }
                    ],
                    "artifacts": [],
                }
            ),
            encoding="utf-8",
        )
        restored = self.store.load_outputs("legacy")
        self.assertEqual(restored.schema_version, 1)
        self.assertEqual(restored.metrology[0].metric, "thickness")

    def test_unsupported_outputs_schema_version_raises(self) -> None:
        directory = self.store.step_dir("bad-outputs")
        directory.mkdir(parents=True)
        (directory / "outputs.json").write_text(
            json.dumps({"schemaVersion": OUTPUTS_SCHEMA_VERSION + 1, "metrology": [], "artifacts": []}),
            encoding="utf-8",
        )
        with self.assertRaises(StateStoreError) as raised:
            self.store.load_outputs("bad-outputs")
        message = str(raised.exception)
        self.assertIn("outputs schemaVersion", message)
        self.assertIn(str(OUTPUTS_SCHEMA_VERSION + 1), message)

        # A state schema version must never be accepted as an outputs version.
        with mock.patch.object(storage, "STATE_SCHEMA_VERSION", OUTPUTS_SCHEMA_VERSION + 1):
            with self.assertRaises(StateStoreError):
                self.store.load_outputs("bad-outputs")

    def test_output_contract_schema_is_json_serializable(self) -> None:
        schema = output_contract_schema()
        self.assertEqual(schema["schemaVersion"], 1)
        self.assertEqual(json.loads(json.dumps(schema, allow_nan=False)), schema)
        # The contract may grow (new state types); storage only needs the
        # value-carrying types it persists to stay described.
        self.assertLessEqual(
            {"StepResult", "StateDelta", "OutputSet", "MetrologyRecord", "Artifact", "CellSetState", "Field", "Layer"},
            set(schema["types"]),
        )
        for name, description in schema["types"].items():
            self.assertTrue(description["fields"], name)
        self.assertIn("vtu", schema["types"]["Artifact"]["fields"]["kind"]["enum"])
        self.assertIn("data", schema["types"]["Field"]["fields"])
        self.assertIsNot(output_contract_schema(), schema)


if __name__ == "__main__":
    unittest.main()
