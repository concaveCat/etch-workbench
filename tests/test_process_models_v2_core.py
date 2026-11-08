"""Core tests for the v2 process-model contract.

These tests use dummy in-memory variants and monkeypatched voxel conversions.
They do not import ViennaPS, start a server, or touch the workbench data
directory.
"""

from __future__ import annotations

import dataclasses
import json
import unittest
from unittest import mock

import numpy as np
from pydantic import BaseModel

import process_models.v2.registry as registry_module
from process_models.v2 import (
    Artifact,
    CellSetState,
    DefaultStateAdapter,
    EngineCapabilities,
    EngineRequirement,
    EngineUnavailableError,
    ExecutionRequest,
    ExecutionResolutionError,
    Field,
    GridSpec,
    Issue,
    Layer,
    LayerState,
    LevelSetState,
    MaterialState,
    MetrologyRecord,
    ModelDescriptor,
    OutputSet,
    ProcessContext,
    ProcessRegistry,
    ProcessSpec,
    ProcessState,
    StateDelta,
    StateMissingError,
    StaticEngine,
    StaticEngineProvider,
    StepResult,
    VariantCapabilities,
    VariantDescriptor,
    VariantRegistration,
    VoxelState,
    compute_implementation_hash,
    resolve_execution,
    version_satisfies,
)


# ---------------------------------------------------------------------------
# dummy contract implementations
# ---------------------------------------------------------------------------
class DummyParams(BaseModel):
    amount: float = 1.0


class OtherParams(BaseModel):
    amount: float = 1.0


_DUMMY_CAPS = VariantCapabilities(
    dimensions=(2, 3),
    reads=frozenset({"geometry"}),
    writes=frozenset({"geometry"}),
    supports_masks=True,
)

_VOXEL_CAPS = VariantCapabilities(
    dimensions=(2, 3),
    reads=frozenset({"geometry", "voxels"}),
    writes=frozenset({"voxels"}),
    geometry_constraints=("height-field",),
    experimental=True,
)

_ALL_CAPS = VariantCapabilities(
    dimensions=(2, 3),
    reads=frozenset({"geometry", "voxels", "fields"}),
    writes=frozenset({"geometry", "voxels", "fields"}),
    geometry_constraints=("height-field",),
    experimental=True,
)

_FIELD_CAPS = VariantCapabilities(
    dimensions=(2, 3),
    reads=frozenset({"fields"}),
    writes=frozenset({"fields"}),
)

_LAYER_CAPS = VariantCapabilities(
    dimensions=(2, 3),
    reads=frozenset({"layers"}),
    writes=frozenset({"layers"}),
)

_STRUCTURED_CAPS = VariantCapabilities(
    dimensions=(2, 3),
    reads=frozenset({"layers", "fields"}),
    writes=frozenset({"layers", "fields"}),
    output_kinds=("metrology", "artifact:profile"),
)

_MATERIAL_CAPS = VariantCapabilities(
    dimensions=(2, 3),
    reads=frozenset({"geometry"}),
    writes=frozenset({"geometry", "materials"}),
)


class DummyVariant:
    variant_id = "dummy"
    version = "1.0.0"
    capabilities = _DUMMY_CAPS
    engine = EngineRequirement(kind="native", engine_id="dummy")
    implementation_hash = None

    def __init__(self) -> None:
        self.execute_calls = 0
        self.last_engine = None

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        return []

    def execute(self, state: ProcessState, params: BaseModel, ctx: ProcessContext, engine: object) -> StepResult:
        self.execute_calls += 1
        self.last_engine = engine
        return StepResult(delta=StateDelta())


class DummyVariantV2(DummyVariant):
    version = "2.0.0"


class ScriptedVariant(DummyVariant):
    variant_id = "scripted"

    def __init__(self, issues: list[Issue] | None = None) -> None:
        super().__init__()
        self.issues = list(issues or [])

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        return list(self.issues)


class VoxelVariant(DummyVariant):
    variant_id = "voxel"
    capabilities = _VOXEL_CAPS


class FieldVariant(DummyVariant):
    variant_id = "field"
    capabilities = _FIELD_CAPS


class LayerVariant(DummyVariant):
    variant_id = "layer"
    capabilities = _LAYER_CAPS


def _spec(version: str = "1.0.0", process_id: str = "dummy.deposit") -> ProcessSpec:
    return ProcessSpec(
        id=process_id,
        version=version,
        label="Dummy deposition",
        params_model=DummyParams,
        dimensions=(2, 3),
        operations=("deposit",),
        description="test fixture",
    )


def _registration(
    variant: object | None = None,
    *,
    process_id: str = "dummy.deposit",
    engine: EngineRequirement | None = None,
    implementation_hash: str | None = None,
) -> VariantRegistration:
    variant = variant or DummyVariant()
    if engine is not None:
        variant.engine = engine  # type: ignore[attr-defined]
    if implementation_hash is not None:
        variant.implementation_hash = implementation_hash  # type: ignore[attr-defined]
    return VariantRegistration(process_id=process_id, variant=variant)  # type: ignore[arg-type]


def _hook_spec(
    *,
    process_id: str = "dummy.match",
    version: str = "1.0.0",
    default_variant: str | None = None,
    parse_step: object = None,
) -> ProcessSpec:
    return ProcessSpec(
        id=process_id,
        version=version,
        label="Dummy matched process",
        params_model=DummyParams,
        dimensions=(2, 3),
        operations=("deposit",),
        default_variant=default_variant,
        parse_step=parse_step,  # type: ignore[arg-type]
    )


def _parser(
    result: DummyParams | None,
    calls: list[tuple[dict, dict]] | None = None,
):
    def parse_step(project: dict, step: dict) -> DummyParams | None:
        if calls is not None:
            calls.append((project, step))
        return result

    return parse_step


def _static_engine(
    *,
    kind: str = "native",
    engine_id: str = "dummy",
    version: str = "1.2.3",
    backends: tuple[str, ...] = ("cpu",),
    capabilities: tuple[str, ...] = (),
    fingerprint: str = "fingerprint-1",
) -> StaticEngine:
    return StaticEngine(
        capabilities=EngineCapabilities(
            engine_id=engine_id,
            version=version,
            kind=kind,
            backends=backends,
            capabilities=capabilities,
        ),
        handle=object(),
        fingerprint=fingerprint,
    )


def _state(
    *,
    dimension: int = 2,
    material_order: tuple[int, ...] = (0,),
    materials: MaterialState | None = None,
) -> ProcessState:
    return ProcessState(
        geometry=LevelSetState(handle=object(), dimension=dimension, grid_delta=0.5),
        materials=materials if materials is not None else MaterialState(order=material_order, names={0: "Si"}),
    )


def _context(dimension: int = 2) -> ProcessContext:
    return ProcessContext(
        dimension=dimension,
        grid_delta=0.5,
        project={"name": "dummy"},
        step={"type": "deposit"},
    )


def _resolver_fixture(
    variant: object | None = None,
    *,
    engine: EngineRequirement | None = None,
) -> tuple[ProcessRegistry, StaticEngineProvider, ProcessSpec]:
    registry = ProcessRegistry()
    spec = _spec()
    registry.register_process(spec)
    registry.register_variant(_registration(variant, engine=engine))
    provider = StaticEngineProvider([_static_engine()])
    return registry, provider, spec


class FakeGrid:
    def __init__(self, material_order: tuple[int, ...] = (0,)) -> None:
        self.material_order = material_order


def _grid(dimension: int = 2, shape: tuple[int, ...] = (4, 3), spacing: float = 0.5) -> GridSpec:
    return GridSpec(dimension=dimension, origin=(0.0,) * dimension, spacing=spacing, shape=shape)


def _field(
    name: str = "temperature",
    *,
    unit: str = "K",
    association: str = "cell",
    dtype: str = "float64",
    shape: tuple[int, ...] = (4, 3),
    components: int = 1,
    data: object = None,
) -> Field:
    return Field(
        name=name,
        unit=unit,
        association=association,  # type: ignore[arg-type]
        dtype=dtype,
        shape=shape,
        components=components,
        data=data,
    )


def _cellset(
    *,
    shape: tuple[int, ...] = (4, 3),
    fields: dict[str, Field] | None = None,
    material_ids: object = None,
) -> CellSetState:
    return CellSetState(
        grid=_grid(dimension=len(shape), shape=shape),
        material_ids=np.zeros(shape, dtype=np.int32) if material_ids is None else material_ids,
        fields=fields if fields is not None else {},
        provenance={"producer": "test"},
    )


def _layers(*, layer_ids: tuple[str, ...] = ("oxide", "nitride")) -> LayerState:
    names = {"oxide": ("SiO2", 1), "nitride": ("Si3N4", 2), "metal": ("W", 3)}
    return LayerState(
        layers=tuple(
            Layer(layer_id=layer_id, material_id=names[layer_id][1], name=names[layer_id][0], thickness=2.0)
            for layer_id in layer_ids
        )
    )


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
class StateTest(unittest.TestCase):
    def test_material_state_validation_and_lookup(self) -> None:
        materials = MaterialState(order=(0, 1), names={0: "Si", 1: "SiO2"})
        self.assertEqual(materials.name_for(0), "Si")
        self.assertEqual(materials.name_for(1), "SiO2")
        self.assertIsNone(materials.name_for(9))
        self.assertEqual(materials.id_for("SiO2"), 1)
        self.assertIsNone(materials.id_for("Si3N4"))

        with self.assertRaises(ValueError):
            MaterialState(order=(-1,))
        # Multiple level sets may share one native material ID (two SiO2
        # depositions); the order is per level-set index, not a set.
        repeated = MaterialState(order=(10, 30, 30), names={10: "Si", 30: "SiO2"})
        self.assertEqual(repeated.order, (10, 30, 30))
        self.assertEqual(repeated.name_for(30), "SiO2")
        self.assertEqual(repeated.id_for("SiO2"), 30)
        with self.assertRaises(ValueError):
            MaterialState(order=(0,), names={1: "SiO2"})
        with self.assertRaises(ValueError):
            MaterialState(order=(0,), names={0: "  "})
        with self.assertRaises(ValueError):
            MaterialState(order=(0, 1), names={0: "Si", 1: "Si"})

    def test_levelset_state_validation(self) -> None:
        state = LevelSetState(handle=object(), dimension=3, grid_delta=0.25)
        self.assertEqual(state.dimension, 3)
        self.assertFalse(hasattr(state, "material_order"))
        with self.assertRaises(ValueError):
            LevelSetState(handle=object(), dimension=4, grid_delta=0.5)
        with self.assertRaises(ValueError):
            LevelSetState(handle=object(), dimension=2, grid_delta=0.0)
        with self.assertRaises(ValueError):
            LevelSetState(handle=object(), dimension=2, grid_delta=float("nan"))

    def test_process_state_is_frozen_and_has_no_engine_or_meta(self) -> None:
        state = _state()
        self.assertEqual(
            {field.name for field in dataclasses.fields(ProcessState)},
            {"geometry", "materials", "layers", "fields", "voxels"},
        )
        self.assertFalse(hasattr(state, "engine"))
        self.assertFalse(hasattr(state, "meta"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            state.fields = _cellset()  # type: ignore[misc]

        replaced = dataclasses.replace(state, voxels=VoxelState(grid=FakeGrid()))
        self.assertIsNot(replaced, state)
        self.assertIsNone(state.voxels)

    def test_process_state_has_require_and_missing_error(self) -> None:
        empty = ProcessState()
        self.assertFalse(empty.has("geometry"))
        # Materials are always present, even when the order is empty.
        self.assertTrue(empty.has("materials"))
        self.assertIs(empty.require("materials"), empty.materials)
        self.assertEqual(empty.materials.order, ())
        self.assertFalse(empty.has("layers"))
        self.assertFalse(empty.has("fields"))
        self.assertFalse(empty.has("voxels"))
        for kind in ("geometry", "layers", "fields"):
            with self.assertRaises(StateMissingError):
                empty.require(kind)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            empty.has("bogus")  # type: ignore[arg-type]

        state = dataclasses.replace(_state(), fields=_cellset(), layers=_layers())
        self.assertTrue(state.has("geometry"))
        self.assertTrue(state.has("materials"))
        self.assertTrue(state.has("layers"))
        self.assertTrue(state.has("fields"))
        self.assertIs(state.require("geometry"), state.geometry)
        self.assertIs(state.require("materials"), state.materials)
        self.assertIs(state.require("layers"), state.layers)
        self.assertIs(state.require("fields"), state.fields)

    def test_state_delta_is_empty_covers_every_flow_representation(self) -> None:
        self.assertEqual(
            {field.name for field in dataclasses.fields(StateDelta)},
            {"geometry", "materials", "layers", "fields", "voxels"},
        )
        self.assertTrue(StateDelta().is_empty())
        self.assertFalse(StateDelta(geometry=LevelSetState(handle=object(), dimension=2, grid_delta=0.5)).is_empty())
        self.assertFalse(StateDelta(materials=MaterialState(order=(0,))).is_empty())
        self.assertFalse(StateDelta(layers=_layers()).is_empty())
        self.assertFalse(StateDelta(fields=_cellset()).is_empty())
        self.assertFalse(StateDelta(voxels=VoxelState(grid=FakeGrid())).is_empty())

    def test_process_context_validation_and_no_materials(self) -> None:
        with self.assertRaises(ValueError):
            ProcessContext(dimension=1, grid_delta=0.5)
        with self.assertRaises(ValueError):
            ProcessContext(dimension=2, grid_delta=0.0)

        context = ProcessContext(dimension=2, grid_delta=0.5)
        self.assertFalse(hasattr(context, "materials"))
        with self.assertRaises(TypeError):
            ProcessContext(dimension=2, grid_delta=0.5, materials=MaterialState(order=(0,)))  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# structured state: grid, fields, cell sets, layers, outputs
# ---------------------------------------------------------------------------
class GridSpecTest(unittest.TestCase):
    def test_grid_spec_normalizes_values(self) -> None:
        grid = GridSpec(dimension=2, origin=(0, 1.5), spacing=0.25, shape=(4, np.int64(3)))
        self.assertEqual(grid.dimension, 2)
        self.assertEqual(grid.origin, (0.0, 1.5))
        self.assertEqual(grid.spacing, 0.25)
        self.assertEqual(grid.shape, (4, 3))
        self.assertIsInstance(grid.shape[1], int)

        three_d = _grid(dimension=3, shape=(2, 3, 4))
        self.assertEqual(three_d.shape, (2, 3, 4))

    def test_grid_spec_validation(self) -> None:
        with self.assertRaises(ValueError):
            GridSpec(dimension=1, origin=(0.0,), spacing=0.5, shape=(4,))
        with self.assertRaises(ValueError):
            GridSpec(dimension=2, origin=(0.0,), spacing=0.5, shape=(4, 4))
        with self.assertRaises(ValueError):
            GridSpec(dimension=2, origin=(0.0, 0.0), spacing=0.5, shape=(4,))
        with self.assertRaises(ValueError):
            GridSpec(dimension=2, origin=(0.0, 0.0), spacing=0.0, shape=(4, 4))
        with self.assertRaises(ValueError):
            GridSpec(dimension=2, origin=(0.0, 0.0), spacing=-0.5, shape=(4, 4))
        with self.assertRaises(ValueError):
            GridSpec(dimension=2, origin=(0.0, 0.0), spacing=float("nan"), shape=(4, 4))
        with self.assertRaises(ValueError):
            GridSpec(dimension=2, origin=(0.0, float("inf")), spacing=0.5, shape=(4, 4))
        with self.assertRaises(ValueError):
            GridSpec(dimension=2, origin=(0.0, 0.0), spacing=0.5, shape=(0, 4))
        with self.assertRaises(ValueError):
            GridSpec(dimension=2, origin=(0.0, 0.0), spacing=0.5, shape=(2.5, 4))


class FieldTest(unittest.TestCase):
    def test_scalar_field_with_numpy_data(self) -> None:
        data = np.zeros((4, 3), dtype=np.float64)
        field = _field(data=data)
        self.assertEqual(field.name, "temperature")
        self.assertEqual(field.shape, (4, 3))
        self.assertEqual(field.components, 1)
        self.assertIs(field.data, data)

    def test_vector_and_tensor_component_shapes(self) -> None:
        vector = _field(name="flux", components=3, data=np.zeros((3, 4, 3), dtype=np.float64))
        self.assertEqual(vector.components, 3)

        tensor = _field(name="stress", components=6, data=np.zeros((6, 4, 3), dtype=np.float64))
        self.assertEqual(tensor.components, 6)

        with self.assertRaises(ValueError):
            _field(name="flux", components=3, data=np.zeros((4, 3), dtype=np.float64))
        with self.assertRaises(ValueError):
            _field(name="stress", components=6, data=np.zeros((9, 4, 3), dtype=np.float64))

    def test_field_numpy_dtype_and_shape_must_match(self) -> None:
        with self.assertRaises(ValueError):
            _field(dtype="float32", data=np.zeros((4, 3), dtype=np.float64))
        with self.assertRaises(ValueError):
            _field(data=np.zeros((3, 4), dtype=np.float64))

    def test_field_validation_and_opaque_handles(self) -> None:
        with self.assertRaises(ValueError):
            _field(name=" ")
        with self.assertRaises(ValueError):
            _field(unit="")
        with self.assertRaises(ValueError):
            _field(association="vertex")
        with self.assertRaises(ValueError):
            _field(dtype="not-a-dtype")
        with self.assertRaises(ValueError):
            _field(components=0)

        handle = object()
        # Non-array data is trusted: shape/dtype are only the contract.
        field = _field(name="density", shape=(99, 99), data=handle)
        self.assertIs(field.data, handle)


class CellSetStateTest(unittest.TestCase):
    def test_cell_set_with_fields_and_numpy_material_ids(self) -> None:
        material_ids = np.zeros((4, 3), dtype=np.int32)
        field = _field(data=np.zeros((4, 3), dtype=np.float64))
        cell_set = CellSetState(
            grid=_grid(),
            material_ids=material_ids,
            fields={"temperature": field},
            provenance={"producer": "test"},
        )
        self.assertIs(cell_set.material_ids, material_ids)
        self.assertEqual(cell_set.fields["temperature"].name, "temperature")
        self.assertEqual(dict(cell_set.provenance), {"producer": "test"})

    def test_cell_set_accepts_opaque_material_ids(self) -> None:
        handle = object()
        cell_set = CellSetState(grid=_grid(dimension=3, shape=(2, 3, 4)), material_ids=handle)
        self.assertIs(cell_set.material_ids, handle)

    def test_cell_set_validation(self) -> None:
        with self.assertRaises(TypeError):
            CellSetState(grid="not-a-grid", material_ids=None)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            CellSetState(grid=_grid(), material_ids=np.zeros((3, 4), dtype=np.int32))
        with self.assertRaises(ValueError):
            CellSetState(grid=_grid(), material_ids=np.zeros((4, 3), dtype=np.float64))

        good = _field(data=np.zeros((4, 3), dtype=np.float64))
        with self.assertRaises(ValueError):
            CellSetState(grid=_grid(), material_ids=None, fields={"other": good})
        with self.assertRaises(ValueError):
            CellSetState(
                grid=_grid(),
                material_ids=None,
                fields={"temperature": _field(association="point")},
            )
        with self.assertRaises(ValueError):
            CellSetState(
                grid=_grid(),
                material_ids=None,
                fields={"temperature": _field(shape=(2, 2))},
            )
        with self.assertRaises(TypeError):
            CellSetState(grid=_grid(), material_ids=None, fields={"temperature": "not-a-field"})  # type: ignore[dict-item]


class LayerStateTest(unittest.TestCase):
    def test_layer_defaults_and_layer_state_order(self) -> None:
        layer = Layer(layer_id="gate", material_id=2, name="PolySi")
        self.assertIsNone(layer.source_step)
        self.assertIsNone(layer.thickness)
        self.assertEqual(dict(layer.metadata), {})

        state = _layers()
        self.assertEqual([item.layer_id for item in state.layers], ["oxide", "nitride"])
        self.assertEqual([item.material_id for item in state.layers], [1, 2])

    def test_layer_validation(self) -> None:
        with self.assertRaises(ValueError):
            Layer(layer_id="", material_id=0, name="Si")
        with self.assertRaises(ValueError):
            Layer(layer_id="si", material_id=-1, name="Si")
        with self.assertRaises(ValueError):
            Layer(layer_id="si", material_id=0, name=" ")
        with self.assertRaises(ValueError):
            Layer(layer_id="si", material_id=0, name="Si", source_step="")
        for thickness in (0.0, -1.0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                Layer(layer_id="si", material_id=0, name="Si", thickness=thickness)

    def test_layer_state_requires_unique_ids(self) -> None:
        with self.assertRaises(ValueError):
            LayerState(
                layers=(
                    Layer(layer_id="oxide", material_id=1, name="SiO2"),
                    Layer(layer_id="oxide", material_id=2, name="Si3N4"),
                )
            )
        state = LayerState(layers=())
        self.assertEqual(state.layers, ())


class MetrologyRecordTest(unittest.TestCase):
    def test_metrology_defaults_and_values(self) -> None:
        record = MetrologyRecord(metric="thickness", value=12.5, unit="nm")
        self.assertEqual(record.method, "plugin")
        self.assertIsNone(record.region)
        self.assertIsNone(record.uncertainty)
        self.assertEqual(record.value, 12.5)

        explicit = MetrologyRecord(
            metric="cd",
            value=8,
            unit="nm",
            region="fin",
            method="reference",
            uncertainty=0.25,
        )
        self.assertEqual(explicit.value, 8.0)
        self.assertEqual(explicit.uncertainty, 0.25)

    def test_metrology_validation(self) -> None:
        with self.assertRaises(ValueError):
            MetrologyRecord(metric=" ", value=1.0, unit="nm")
        with self.assertRaises(ValueError):
            MetrologyRecord(metric="thickness", value=float("nan"), unit="nm")
        with self.assertRaises(ValueError):
            MetrologyRecord(metric="thickness", value=float("inf"), unit="nm")
        with self.assertRaises(ValueError):
            MetrologyRecord(metric="thickness", value=1.0, unit="")
        with self.assertRaises(ValueError):
            MetrologyRecord(metric="thickness", value=1.0, unit="nm", region="")
        with self.assertRaises(ValueError):
            MetrologyRecord(metric="thickness", value=1.0, unit="nm", method="")

        for uncertainty in (-0.1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                MetrologyRecord(metric="thickness", value=1.0, unit="nm", uncertainty=uncertainty)


class OutputSetTest(unittest.TestCase):
    def test_new_artifact_kinds_and_schema_version(self) -> None:
        for kind in ("table", "profile", "device", "image", "surface_mesh", "native"):
            artifact = Artifact(kind=kind, name=f"{kind}.dat")  # type: ignore[arg-type]
            self.assertEqual(artifact.schema_version, 1)
            self.assertIsNone(artifact.path)

        versioned = Artifact(kind="profile", name="depth.profile", path="out/depth.profile", schema_version=2)
        self.assertEqual(versioned.schema_version, 2)
        self.assertEqual(versioned.path, "out/depth.profile")

    def test_artifact_validation(self) -> None:
        with self.assertRaises(ValueError):
            Artifact(kind="bogus", name="x")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Artifact(kind="json", name=" ")
        with self.assertRaises(ValueError):
            Artifact(kind="json", name="x", path="")
        with self.assertRaises(ValueError):
            Artifact(kind="json", name="x", schema_version=0)
        with self.assertRaises(ValueError):
            Artifact(kind="json", name="x", schema_version=1.5)  # type: ignore[arg-type]

    def test_output_set_defaults_and_validation(self) -> None:
        outputs = OutputSet()
        self.assertEqual(outputs.schema_version, 1)
        self.assertEqual(outputs.metrology, ())
        self.assertEqual(outputs.artifacts, ())

        record = MetrologyRecord(metric="thickness", value=10.0, unit="nm")
        artifact = Artifact(kind="table", name="rates.csv")
        populated = OutputSet(schema_version=2, metrology=(record,), artifacts=(artifact,))
        self.assertEqual(populated.schema_version, 2)
        self.assertEqual(populated.metrology, (record,))
        self.assertEqual(populated.artifacts, (artifact,))

        with self.assertRaises(ValueError):
            OutputSet(schema_version=0)
        with self.assertRaises(TypeError):
            OutputSet(metrology=(record, "not-a-record"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            OutputSet(artifacts=("not-an-artifact",))  # type: ignore[arg-type]

    def test_step_result_separates_state_from_report_data(self) -> None:
        defaults = StepResult(delta=StateDelta())
        self.assertEqual(dict(defaults.metadata), {})
        self.assertEqual(defaults.warnings, [])
        self.assertEqual(defaults.outputs, OutputSet())
        self.assertTrue(defaults.delta.is_empty())

        record = MetrologyRecord(metric="thickness", value=3.0, unit="nm")
        artifact = Artifact(kind="profile", name="depth.profile")
        result = StepResult(
            delta=StateDelta(layers=_layers()),
            outputs=OutputSet(metrology=(record,), artifacts=(artifact,)),
            metadata={"model": "dummy"},
            warnings=["clipped"],
        )
        self.assertFalse(result.delta.is_empty())
        self.assertEqual(result.metadata["model"], "dummy")
        self.assertEqual(result.warnings, ["clipped"])
        self.assertEqual(result.outputs.metrology[0].metric, "thickness")
        self.assertEqual([item.name for item in result.outputs.artifacts], ["depth.profile"])
        # Report data never leaks into the flow delta.
        self.assertIsNone(result.delta.geometry)
        self.assertIsNone(result.delta.fields)
        self.assertIsNone(result.delta.voxels)


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
class EngineTest(unittest.TestCase):
    def test_static_engine_matches_version_bounds(self) -> None:
        engine = _static_engine(version="4.7.0")

        lease = engine.acquire(EngineRequirement(kind="native", engine_id="dummy", min_version="4.6.2"))
        self.assertEqual(lease.engine_id, "dummy")
        self.assertEqual(lease.engine_version, "4.7.0")
        self.assertEqual(lease.fingerprint, "fingerprint-1")
        self.assertIsNotNone(lease.handle)
        self.assertEqual(lease.backend, "cpu")

        engine.acquire(EngineRequirement(kind="native", engine_id="dummy", max_version="4.7.0"))
        with self.assertRaises(EngineUnavailableError):
            engine.acquire(EngineRequirement(kind="native", engine_id="dummy", min_version="5.0.0"))
        with self.assertRaises(EngineUnavailableError):
            engine.acquire(EngineRequirement(kind="native", engine_id="dummy", max_version="4.6.2"))

    def test_static_engine_rejects_kind_backend_and_capability_mismatch(self) -> None:
        engine = _static_engine(version="1.0.0", backends=("cpu",))
        with self.assertRaises(EngineUnavailableError):
            engine.acquire(EngineRequirement(kind="surrogate", engine_id="dummy"))
        with self.assertRaises(EngineUnavailableError):
            engine.acquire(EngineRequirement(kind="native", engine_id="other"))
        with self.assertRaises(EngineUnavailableError):
            engine.acquire(EngineRequirement(kind="native", engine_id="dummy", backend="gpu"))
        with self.assertRaises(EngineUnavailableError):
            engine.acquire(EngineRequirement(kind="native", engine_id="dummy", capabilities=("masks",)))

    def test_backend_any_matches_gpu_only_engine(self) -> None:
        engine = _static_engine(backends=("gpu",))
        lease = engine.acquire(EngineRequirement(kind="native", engine_id="dummy", backend="any"))
        self.assertEqual(lease.backend, "gpu")
        gpu_lease = engine.acquire(EngineRequirement(kind="native", engine_id="dummy", backend="gpu"))
        self.assertEqual(gpu_lease.backend, "gpu")

    def test_version_satisfies_non_semver_requires_exact_bound(self) -> None:
        self.assertTrue(version_satisfies("4.7.0", "4.6.2", None))
        self.assertTrue(version_satisfies("4.7.0", None, "4.7.0"))
        self.assertFalse(version_satisfies("4.6.1", "4.6.2", None))
        self.assertTrue(version_satisfies("rev-a", "rev-a", None))
        self.assertFalse(version_satisfies("rev-a", "rev-b", None))
        self.assertFalse(version_satisfies("4.7.0", "rev-a", None))

    def test_static_engine_provider_first_match_and_no_fallback(self) -> None:
        cpu = _static_engine(backends=("cpu",), fingerprint="cpu-fp")
        gpu = _static_engine(backends=("gpu",), fingerprint="gpu-fp")
        provider = StaticEngineProvider([cpu, gpu])

        self.assertEqual([caps.backends for caps in provider.engines()], [("cpu",), ("gpu",)])
        self.assertEqual(provider.acquire(EngineRequirement(kind="native", engine_id="dummy")).fingerprint, "cpu-fp")
        self.assertEqual(
            provider.acquire(EngineRequirement(kind="native", engine_id="dummy", backend="gpu")).fingerprint,
            "gpu-fp",
        )

        with self.assertRaises(EngineUnavailableError):
            provider.acquire(EngineRequirement(kind="native", engine_id="missing"))
        gpu_only = StaticEngineProvider([_static_engine(backends=("gpu",))])
        with self.assertRaises(EngineUnavailableError):
            gpu_only.acquire(EngineRequirement(kind="native", engine_id="dummy", backend="cpu"))

    def test_static_lease_release_is_a_noop_and_supports_context_manager(self) -> None:
        with _static_engine().acquire(EngineRequirement(kind="native", engine_id="dummy")) as lease:
            self.assertEqual(lease.engine_id, "dummy")
        lease.release()
        lease.release()


# ---------------------------------------------------------------------------
# contract + registry
# ---------------------------------------------------------------------------
class ContractRegistryTest(unittest.TestCase):
    def test_list_processes_builds_descriptor_without_spec_method(self) -> None:
        self.assertFalse(hasattr(ProcessSpec, "describe"))

        registry = ProcessRegistry()
        registry.register_process(_spec())
        registry.register_variant(_registration(DummyVariant()))

        descriptor = registry.list_processes()[0]
        self.assertEqual(descriptor.id, "dummy.deposit")
        self.assertEqual(descriptor.version, "1.0.0")
        self.assertEqual(descriptor.dimensions, (2, 3))
        self.assertEqual(descriptor.operations, ("deposit",))
        self.assertEqual([variant.id for variant in descriptor.variants], ["dummy"])
        self.assertEqual(descriptor.description, "test fixture")

    def test_variant_registration_derives_identity(self) -> None:
        variant = DummyVariant()
        registration = VariantRegistration(process_id="dummy.deposit", variant=variant)

        self.assertEqual(registration.process_id, "dummy.deposit")
        self.assertEqual(registration.variant_id, "dummy")
        self.assertEqual(registration.version, "1.0.0")
        self.assertIs(registration.capabilities, variant.capabilities)
        self.assertIs(registration.capabilities, _DUMMY_CAPS)
        self.assertIs(registration.engine, variant.engine)
        self.assertIsNone(registration.declared_hash)

        variant.implementation_hash = "sha256:declared"
        self.assertEqual(registration.declared_hash, "sha256:declared")

    def test_variant_registration_validation(self) -> None:
        with self.assertRaises(ValueError):
            VariantRegistration(process_id=" ", variant=DummyVariant())

        class BlankIdVariant(DummyVariant):
            variant_id = " "

        with self.assertRaises(ValueError):
            VariantRegistration(process_id="dummy.deposit", variant=BlankIdVariant())

        class BlankVersionVariant(DummyVariant):
            version = ""

        with self.assertRaises(ValueError):
            VariantRegistration(process_id="dummy.deposit", variant=BlankVersionVariant())

        class MissingEngineVariant(DummyVariant):
            engine = None  # type: ignore[assignment]

        with self.assertRaises(TypeError):
            VariantRegistration(process_id="dummy.deposit", variant=MissingEngineVariant())

        class BadCapabilitiesVariant(DummyVariant):
            capabilities = "not-capabilities"  # type: ignore[assignment]

        with self.assertRaises(TypeError):
            VariantRegistration(process_id="dummy.deposit", variant=BadCapabilitiesVariant())

        class BadHashVariant(DummyVariant):
            implementation_hash = 123  # type: ignore[assignment]

        with self.assertRaises(TypeError):
            VariantRegistration(process_id="dummy.deposit", variant=BadHashVariant())

    def test_variant_registration_requires_callable_validate_and_execute(self) -> None:
        class NoValidateVariant(DummyVariant):
            validate = None  # type: ignore[assignment]

        with self.assertRaises(TypeError):
            VariantRegistration(process_id="dummy.deposit", variant=NoValidateVariant())

        class NoExecuteVariant(DummyVariant):
            execute = "not-callable"  # type: ignore[assignment]

        with self.assertRaises(TypeError):
            VariantRegistration(process_id="dummy.deposit", variant=NoExecuteVariant())

        registration = VariantRegistration(process_id="dummy.deposit", variant=DummyVariant())
        self.assertTrue(callable(registration.variant.validate))
        self.assertTrue(callable(registration.variant.execute))

    def test_registry_multiple_process_versions_and_pins(self) -> None:
        registry = ProcessRegistry()
        registry.register_process(_spec("1.0.0"))
        registry.register_process(_spec("2.0.0"))
        registry.register_variant(_registration())

        self.assertEqual(registry.get_process("dummy.deposit").version, "2.0.0")
        self.assertEqual(registry.get_process("dummy.deposit", "1.0.0").version, "1.0.0")
        self.assertEqual(registry.get_variant("dummy.deposit", "dummy").process_version, "2.0.0")
        pinned = registry.get_variant("dummy.deposit", "dummy", expected_process_version="1.0.0")
        self.assertEqual(pinned.process_version, "1.0.0")

    def test_registry_add_variant_without_process_bump(self) -> None:
        registry = ProcessRegistry()
        registry.register_process(_spec("1.0.0"))
        registry.register_variant(_registration(DummyVariant()))

        registry.register_variant(_registration(DummyVariantV2()))

        self.assertEqual(registry.get_process("dummy.deposit").version, "1.0.0")
        self.assertEqual(registry.get_variant("dummy.deposit", "dummy").variant_version, "2.0.0")
        pinned = registry.get_variant(
            "dummy.deposit", "dummy", expected_process_version="1.0.0", expected_variant_version="1.0.0"
        )
        self.assertEqual(pinned.variant_version, "1.0.0")
        descriptors = registry.list_processes()
        self.assertEqual(len(descriptors), 1)
        self.assertEqual([variant.version for variant in descriptors[0].variants], ["1.0.0", "2.0.0"])

    def test_registry_latest_selection_and_registration_order_fallback(self) -> None:
        semver = ProcessRegistry()
        for version in ("1.9.0", "1.10.0", "1.2.0"):
            semver.register_process(_spec(version))
        self.assertEqual(semver.get_process("dummy.deposit").version, "1.10.0")

        commit_style = ProcessRegistry()
        for version in ("rev-a", "rev-b", "rev-c"):
            commit_style.register_process(_spec(version))
        self.assertEqual(commit_style.get_process("dummy.deposit").version, "rev-c")

    def test_registry_duplicate_keys_raise_value_error(self) -> None:
        registry = ProcessRegistry()
        registry.register_process(_spec("1.0.0"))
        with self.assertRaises(ValueError):
            registry.register_process(_spec("1.0.0"))

        registry.register_variant(_registration(DummyVariant()))
        with self.assertRaises(ValueError):
            registry.register_variant(_registration(DummyVariant()))

    def test_registry_unknown_keys_raise_key_error(self) -> None:
        registry = ProcessRegistry()
        with self.assertRaises(KeyError):
            registry.get_process("missing")
        with self.assertRaises(KeyError):
            registry.register_variant(_registration())

        registry.register_process(_spec("1.0.0"))
        with self.assertRaises(KeyError):
            registry.get_process("missing")
        with self.assertRaises(KeyError):
            registry.get_process("dummy.deposit", "9.9.9")
        with self.assertRaises(KeyError):
            registry.get_variant("dummy.deposit", "missing")
        with self.assertRaises(KeyError):
            registry.list_variants("missing")

        registry.register_variant(_registration())
        with self.assertRaises(KeyError):
            registry.get_variant("dummy.deposit", "dummy", expected_variant_version="9.9.9")

    def test_registry_descriptors_include_variants(self) -> None:
        registry = ProcessRegistry()
        registry.register_process(_spec())
        registry.register_variant(_registration(DummyVariant()))
        registry.register_variant(_registration(VoxelVariant()))

        descriptors = registry.list_processes()
        self.assertEqual(len(descriptors), 1)
        variants = {variant.id: variant for variant in descriptors[0].variants}
        self.assertEqual(variants["voxel"].kind, "native")
        self.assertEqual(variants["voxel"].reads, ("geometry", "voxels"))
        self.assertEqual(variants["voxel"].writes, ("voxels",))
        self.assertTrue(variants["voxel"].experimental)
        self.assertEqual(variants["voxel"].execution_profiles, ())
        self.assertIsNone(variants["voxel"].state_profile)
        self.assertEqual(len(registry.list_variants("dummy.deposit")), 2)

    def test_capabilities_output_kinds_default_and_descriptor(self) -> None:
        self.assertEqual(_DUMMY_CAPS.output_kinds, ())
        with self.assertRaises(ValueError):
            VariantCapabilities(
                dimensions=(2,),
                reads=frozenset(),
                writes=frozenset(),
                output_kinds=("",),
            )

        registry = ProcessRegistry()
        registry.register_process(_spec())
        variant = DummyVariant()
        variant.capabilities = _STRUCTURED_CAPS
        registry.register_variant(_registration(variant))

        descriptor = registry.list_variants("dummy.deposit")[0]
        self.assertEqual(descriptor.output_kinds, ("metrology", "artifact:profile"))
        self.assertEqual(descriptor.execution_profiles, ())

        bare = VariantDescriptor(
            id="dummy",
            version="1.0.0",
            kind="native",
            engine_id="dummy",
            reads=(),
            writes=(),
            experimental=False,
            implementation_hash="x" * 64,
        )
        self.assertEqual(bare.output_kinds, ())

    def test_implementation_hash_is_file_based_and_stable(self) -> None:
        variant = DummyVariant()
        digest = compute_implementation_hash(variant)
        self.assertEqual(len(digest), 64)
        int(digest, 16)
        self.assertEqual(digest, compute_implementation_hash(variant))
        self.assertNotEqual(digest, compute_implementation_hash(DummyVariantV2()))

        # Forcing the "no source file" fallback must change the digest: the
        # file bytes (including module-level helpers) were part of it.
        with mock.patch.object(registry_module, "_source_file", return_value=None):
            class_only = compute_implementation_hash(variant)
        self.assertEqual(len(class_only), 64)
        self.assertNotEqual(class_only, digest)

    def test_registry_uses_declared_or_computed_hash(self) -> None:
        registry = ProcessRegistry()
        registry.register_process(_spec())
        registry.register_variant(_registration(DummyVariant(), implementation_hash="sha256:explicit"))
        self.assertEqual(
            registry.get_variant("dummy.deposit", "dummy").implementation_hash,
            "sha256:explicit",
        )
        self.assertEqual(registry.list_variants("dummy.deposit")[0].implementation_hash, "sha256:explicit")

        computed = ProcessRegistry()
        computed.register_process(_spec())
        computed.register_variant(_registration(DummyVariant()))
        self.assertIsNone(computed._variants["dummy.deposit"]["dummy"][0].declared_hash)
        self.assertEqual(
            computed.get_variant("dummy.deposit", "dummy").implementation_hash,
            compute_implementation_hash(DummyVariant()),
        )
        self.assertEqual(
            computed.list_variants("dummy.deposit")[0].implementation_hash,
            compute_implementation_hash(DummyVariant()),
        )


# ---------------------------------------------------------------------------
# process spec hooks, step parsing, default variants
# ---------------------------------------------------------------------------
class ProcessSpecHooksTest(unittest.TestCase):
    def test_spec_hook_defaults_and_validation(self) -> None:
        defaults = _spec()
        self.assertIsNone(defaults.default_variant)
        self.assertIsNone(defaults.parse_step)

        def parse_step(project: dict, step: dict) -> DummyParams:
            return DummyParams(amount=float(step.get("amount", 1.0)))

        spec = _hook_spec(default_variant="native", parse_step=parse_step)
        self.assertEqual(spec.default_variant, "native")
        self.assertIs(spec.parse_step, parse_step)
        self.assertEqual(spec.parse_step({}, {"amount": 3}), DummyParams(amount=3.0))

        # Hooks are runtime callables, never serializable identity.
        descriptor_fields = {field.name for field in dataclasses.fields(ModelDescriptor)}
        self.assertFalse({"default_variant", "parse_step"} & descriptor_fields)
        registry = ProcessRegistry()
        registry.register_process(spec)
        descriptor = registry.list_processes()[0]
        self.assertEqual(descriptor.id, "dummy.match")
        self.assertNotIn("parse_step", json.dumps(dataclasses.asdict(descriptor)))

    def test_spec_hook_validation(self) -> None:
        for value in ("", " ", 3):
            with self.assertRaises(ValueError):
                _hook_spec(default_variant=value)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _hook_spec(parse_step="not-callable")

    def test_resolve_step_returns_none_without_a_match(self) -> None:
        registry = ProcessRegistry()
        registry.register_process(_spec("1.0.0"))
        registry.register_process(_hook_spec(process_id="dummy.match", parse_step=_parser(None)))
        self.assertIsNone(registry.resolve_step({}, {"model": "isotropic"}))

    def test_resolve_step_returns_the_only_match_with_parsed_params(self) -> None:
        calls: list[tuple[dict, dict]] = []
        registry = ProcessRegistry()
        registry.register_process(_hook_spec(parse_step=_parser(None)))
        registry.register_process(
            _hook_spec(process_id="dummy.real", parse_step=_parser(DummyParams(amount=2.0), calls))
        )
        project = {"dimension": 2}
        step = {"model": "isotropic", "operation": "etch"}

        resolved = registry.resolve_step(project, step)

        self.assertIsNotNone(resolved)
        assert resolved is not None
        spec, params = resolved
        self.assertEqual(spec.id, "dummy.real")
        self.assertEqual(params, DummyParams(amount=2.0))
        self.assertEqual(calls, [(project, step)])

    def test_resolve_step_parses_once_per_candidate(self) -> None:
        calls: list[tuple[dict, dict]] = []
        registry = ProcessRegistry()
        registry.register_process(
            _hook_spec(process_id="dummy.only", parse_step=_parser(DummyParams(amount=4.0), calls))
        )

        resolved = registry.resolve_step({"name": "p"}, {"amount": 4})

        assert resolved is not None
        self.assertEqual(resolved[1], DummyParams(amount=4.0))
        self.assertEqual(len(calls), 1)

    def test_resolve_step_checks_only_the_newest_process_version(self) -> None:
        matched_old = ProcessRegistry()
        matched_old.register_process(_hook_spec(version="1.0.0", parse_step=_parser(DummyParams())))
        matched_old.register_process(_hook_spec(version="2.0.0", parse_step=_parser(None)))
        self.assertIsNone(matched_old.resolve_step({}, {}))

        matched_new = ProcessRegistry()
        matched_new.register_process(_hook_spec(version="1.0.0", parse_step=_parser(None)))
        matched_new.register_process(_hook_spec(version="2.0.0", parse_step=_parser(DummyParams())))
        resolved = matched_new.resolve_step({}, {})
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved[0].version, "2.0.0")

    def test_resolve_step_rejects_ambiguous_matches(self) -> None:
        registry = ProcessRegistry()
        registry.register_process(_hook_spec(process_id="dummy.a", parse_step=_parser(DummyParams())))
        registry.register_process(_hook_spec(process_id="dummy.b", parse_step=_parser(DummyParams())))

        with self.assertRaises(ValueError) as caught:
            registry.resolve_step({}, {})
        message = str(caught.exception)
        self.assertIn("dummy.a", message)
        self.assertIn("dummy.b", message)

    def test_default_variant_id_prefers_the_explicit_default(self) -> None:
        registry = ProcessRegistry()
        registry.register_process(_hook_spec(process_id="dummy.deposit", default_variant="native"))
        registry.register_variant(_registration(DummyVariant()))
        registry.register_variant(_registration(VoxelVariant()))

        self.assertEqual(registry.default_variant_id("dummy.deposit"), "native")

    def test_default_variant_id_uses_the_only_registered_variant(self) -> None:
        registry = ProcessRegistry()
        registry.register_process(_spec())
        registry.register_variant(_registration(DummyVariant()))
        self.assertEqual(registry.default_variant_id("dummy.deposit"), "dummy")

        # Multiple versions of the same variant id are still one choice.
        registry.register_variant(_registration(DummyVariantV2()))
        self.assertEqual(registry.default_variant_id("dummy.deposit"), "dummy")

    def test_default_variant_id_requires_a_choice(self) -> None:
        registry = ProcessRegistry()
        registry.register_process(_spec())
        with self.assertRaises(KeyError):
            registry.default_variant_id("dummy.deposit")
        with self.assertRaises(KeyError):
            registry.default_variant_id("missing")

        registry.register_variant(_registration(DummyVariant()))
        registry.register_variant(_registration(VoxelVariant()))
        with self.assertRaises(KeyError):
            registry.default_variant_id("dummy.deposit")


# ---------------------------------------------------------------------------
# resolver
# ---------------------------------------------------------------------------
class ResolverTest(unittest.TestCase):
    def test_resolver_success_and_stable_identity(self) -> None:
        registry, provider, spec = _resolver_fixture()
        request = ExecutionRequest(
            process_id="dummy.deposit",
            variant_id="dummy",
            params=DummyParams(amount=2.0),
            context=_context(),
            state=_state(),
        )
        plan = resolve_execution(request, registry, provider)

        self.assertIs(plan.request, request)
        self.assertEqual(plan.process.id, spec.id)
        self.assertEqual(plan.process_version, "1.0.0")
        self.assertEqual(plan.variant_version, "1.0.0")
        self.assertEqual(plan.engine_version, "1.2.3")
        self.assertEqual(plan.engine_fingerprint, "fingerprint-1")
        self.assertEqual(plan.state_adapter.id, "default-v2")
        self.assertEqual(plan.implementation_hash, compute_implementation_hash(DummyVariant()))

        identity = plan.identity()
        self.assertEqual(
            set(identity),
            {
                "process_id",
                "process_version",
                "variant_id",
                "variant_version",
                "implementation_hash",
                "engine_id",
                "engine_version",
                "engine_fingerprint",
                "adapter",
                "engine_backend",
                "engine_kernel",
                "engine_device",
                "engine_strategy",
                "state_backend_id",
                "state_backend_version",
                "state_contract",
            },
        )
        self.assertTrue(all(isinstance(value, str) for value in identity.values()))
        json.dumps(identity)
        self.assertEqual(identity, plan.identity())

        # Resolution must not execute the variant.
        self.assertEqual(plan.variant.variant.execute_calls, 0)

    def test_resolver_rejects_wrong_params_type(self) -> None:
        registry, provider, _ = _resolver_fixture()
        with self.assertRaises(ExecutionResolutionError):
            resolve_execution(
                ExecutionRequest(
                    process_id="dummy.deposit",
                    variant_id="dummy",
                    params=OtherParams(),
                    context=_context(),
                    state=_state(),
                ),
                registry,
                provider,
            )

    def test_resolver_rejects_unsupported_dimension(self) -> None:
        variant = VoxelVariant()
        variant.capabilities = VariantCapabilities(dimensions=(2,), reads=frozenset({"geometry"}), writes=frozenset())
        registry = ProcessRegistry()
        registry.register_process(_spec())
        registry.register_variant(_registration(variant))

        with self.assertRaises(ExecutionResolutionError) as caught:
            resolve_execution(
                ExecutionRequest(
                    process_id="dummy.deposit",
                    variant_id="voxel",
                    params=DummyParams(),
                    context=_context(dimension=3),
                    state=_state(dimension=3),
                ),
                registry,
                StaticEngineProvider([_static_engine()]),
            )
        self.assertIn("dimension 3", str(caught.exception))

    def test_resolver_rejects_missing_state_kind(self) -> None:
        registry, provider, _ = _resolver_fixture(VoxelVariant())
        with self.assertRaises(ExecutionResolutionError) as caught:
            resolve_execution(
                ExecutionRequest(
                    process_id="dummy.deposit",
                    variant_id="voxel",
                    params=DummyParams(),
                    context=_context(),
                    state=ProcessState(),
                ),
                registry,
                provider,
            )
        message = str(caught.exception)
        self.assertIn("geometry", message)
        self.assertIn("voxels", message)

    def test_resolver_allows_voxel_read_derived_from_geometry(self) -> None:
        registry, provider, _ = _resolver_fixture(VoxelVariant())
        state = _state()
        self.assertIsNone(state.voxels)
        plan = resolve_execution(
            ExecutionRequest(
                process_id="dummy.deposit",
                variant_id="voxel",
                params=DummyParams(),
                context=_context(),
                state=state,
            ),
            registry,
            provider,
        )
        self.assertEqual(plan.state_adapter.id, "default-v2")
        self.assertEqual(plan.state_adapter.provides, frozenset({"voxels"}))

    def test_resolver_requires_stored_layers_and_fields(self) -> None:
        # No adapter derives layers/fields; a read without stored state fails.
        registry, provider, _ = _resolver_fixture(LayerVariant())
        with self.assertRaises(ExecutionResolutionError) as caught:
            resolve_execution(
                ExecutionRequest(
                    process_id="dummy.deposit",
                    variant_id="layer",
                    params=DummyParams(),
                    context=_context(),
                    state=_state(),
                ),
                registry,
                provider,
            )
        self.assertIn("layers", str(caught.exception))
        stored = dataclasses.replace(_state(), layers=_layers())
        plan = resolve_execution(
            ExecutionRequest(
                process_id="dummy.deposit",
                variant_id="layer",
                params=DummyParams(),
                context=_context(),
                state=stored,
            ),
            registry,
            provider,
        )
        self.assertEqual(plan.state_adapter.id, "default-v2")
        self.assertEqual(plan.state_adapter.provides, frozenset({"voxels"}))

        field_registry, field_provider, _ = _resolver_fixture(FieldVariant())
        with self.assertRaises(ExecutionResolutionError) as caught:
            resolve_execution(
                ExecutionRequest(
                    process_id="dummy.deposit",
                    variant_id="field",
                    params=DummyParams(),
                    context=_context(),
                    state=_state(),
                ),
                field_registry,
                field_provider,
            )
        self.assertIn("fields", str(caught.exception))
        resolve_execution(
            ExecutionRequest(
                process_id="dummy.deposit",
                variant_id="field",
                params=DummyParams(),
                context=_context(),
                state=dataclasses.replace(_state(), fields=_cellset()),
            ),
            field_registry,
            field_provider,
        )

    def test_resolver_accepts_materials_as_the_only_material_source(self) -> None:
        # Material order lives only on ProcessState.materials; geometry no
        # longer carries a competing order for the resolver to check.
        registry, provider, _ = _resolver_fixture()
        state = _state(materials=MaterialState(order=(0,)))
        self.assertFalse(hasattr(state.geometry, "material_order"))
        plan = resolve_execution(
            ExecutionRequest(
                process_id="dummy.deposit",
                variant_id="dummy",
                params=DummyParams(),
                context=_context(),
                state=state,
            ),
            registry,
            provider,
        )
        self.assertEqual(plan.process.id, "dummy.deposit")
        self.assertEqual(state.require("materials").order, (0,))

    def test_resolver_uses_state_materials_not_context(self) -> None:
        registry, provider, _ = _resolver_fixture()
        context = _context()
        self.assertFalse(hasattr(context, "materials"))
        # A consistent state.materials passes even though the context has no
        # material table at all.
        plan = resolve_execution(
            ExecutionRequest(
                process_id="dummy.deposit",
                variant_id="dummy",
                params=DummyParams(),
                context=context,
                state=_state(materials=MaterialState(order=(0,), names={0: "Si"})),
            ),
            registry,
            provider,
        )
        self.assertEqual(plan.process.id, "dummy.deposit")

    def test_resolver_rejects_any_validation_error_with_all_messages(self) -> None:
        variant = ScriptedVariant(
            [
                Issue(path="amount", message="amount is too small"),
                Issue(path="", message="material table is empty"),
                Issue(path="rate", message="warning only", severity="warning"),
            ]
        )
        registry, provider, _ = _resolver_fixture(variant)
        with self.assertRaises(ExecutionResolutionError) as caught:
            resolve_execution(
                ExecutionRequest(
                    process_id="dummy.deposit",
                    variant_id="scripted",
                    params=DummyParams(),
                    context=_context(),
                    state=_state(),
                ),
                registry,
                provider,
            )
        message = str(caught.exception)
        self.assertIn("amount is too small", message)
        self.assertIn("material table is empty", message)
        self.assertNotIn("warning only", message)

    def test_resolver_maps_engine_unavailable_without_fallback(self) -> None:
        registry = ProcessRegistry()
        registry.register_process(_spec())
        registry.register_variant(
            _registration(
                engine=EngineRequirement(kind="native", engine_id="dummy", min_version="9.0.0"),
            )
        )
        provider = StaticEngineProvider([_static_engine(version="1.2.3", fingerprint="old")])

        with self.assertRaises(ExecutionResolutionError) as caught:
            resolve_execution(
                ExecutionRequest(
                    process_id="dummy.deposit",
                    variant_id="dummy",
                    params=DummyParams(),
                    context=_context(),
                    state=_state(),
                ),
                registry,
                provider,
            )
        self.assertIn("9.0.0", str(caught.exception))


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------
class AdapterTest(unittest.TestCase):
    def test_adapter_provides_only_derived_representations(self) -> None:
        self.assertEqual(DefaultStateAdapter.provides, frozenset({"voxels"}))
        self.assertEqual(DefaultStateAdapter().provides, frozenset({"voxels"}))

    def test_prepare_reads_voxels_from_geometry_without_mutating_input(self) -> None:
        state = _state()
        grid = FakeGrid()
        adapter = DefaultStateAdapter()
        with mock.patch("process_models.v2.adapters._read_voxels", return_value=grid) as read:
            prepared = adapter.prepare(state, _VOXEL_CAPS)

        read.assert_called_once_with(state.geometry.handle)
        self.assertIs(prepared.voxels.grid, grid)
        self.assertIsNone(state.voxels)
        self.assertIsNot(prepared, state)
        self.assertIs(prepared.geometry, state.geometry)

    def test_prepare_rejects_voxels_without_geometry(self) -> None:
        with self.assertRaises(StateMissingError):
            DefaultStateAdapter().prepare(ProcessState(), _VOXEL_CAPS)

    def test_prepare_rejects_missing_fields(self) -> None:
        with self.assertRaises(StateMissingError):
            DefaultStateAdapter().prepare(_state(), _FIELD_CAPS)

        state = dataclasses.replace(_state(), fields=_cellset())
        self.assertIs(DefaultStateAdapter().prepare(state, _FIELD_CAPS), state)

    def test_prepare_rejects_missing_layers_without_derivation(self) -> None:
        with self.assertRaises(StateMissingError) as caught:
            DefaultStateAdapter().prepare(_state(), _LAYER_CAPS)
        self.assertIn("cannot be derived", str(caught.exception))

        state = dataclasses.replace(_state(), layers=_layers())
        self.assertIs(DefaultStateAdapter().prepare(state, _LAYER_CAPS), state)
        self.assertNotIn("layers", DefaultStateAdapter.provides)
        self.assertNotIn("fields", DefaultStateAdapter.provides)

    def test_commit_voxel_delta_writes_back_and_clears_voxels(self) -> None:
        state = dataclasses.replace(_state(), voxels=VoxelState(grid=FakeGrid()))
        grid = FakeGrid(material_order=(0, 1))
        new_handle = object()
        delta = StateDelta(voxels=VoxelState(grid=grid))

        with mock.patch("process_models.v2.adapters._write_voxels", return_value=new_handle) as write:
            committed = DefaultStateAdapter().commit(state, delta, _VOXEL_CAPS)

        write.assert_called_once_with(state.geometry.handle, grid)
        self.assertIs(committed.geometry.handle, new_handle)
        self.assertEqual(committed.geometry.dimension, state.geometry.dimension)
        self.assertEqual(committed.geometry.grid_delta, state.geometry.grid_delta)
        self.assertIsNone(committed.voxels)
        # A voxel write-back replaces geometry only; materials are canonical
        # and unchanged unless the variant also sends a materials delta.
        self.assertIs(committed.materials, state.materials)
        # Input state is untouched.
        self.assertIsNotNone(state.voxels)
        self.assertIsNot(state.geometry, committed.geometry)

    def test_commit_geometry_delta_wins_over_voxels_and_applies_fields(self) -> None:
        state = dataclasses.replace(_state(), voxels=VoxelState(grid=FakeGrid()))
        new_geometry = LevelSetState(handle=object(), dimension=2, grid_delta=0.5)
        fields = _cellset()
        delta = StateDelta(geometry=new_geometry, voxels=VoxelState(grid=FakeGrid()), fields=fields)

        with mock.patch("process_models.v2.adapters._write_voxels") as write:
            committed = DefaultStateAdapter().commit(state, delta, _ALL_CAPS)

        write.assert_not_called()
        self.assertIs(committed.geometry, new_geometry)
        self.assertIs(committed.fields, fields)
        self.assertIsNone(committed.voxels)
        self.assertIs(committed.materials, state.materials)

    def test_commit_geometry_delta_does_not_silently_change_materials(self) -> None:
        state = _state(materials=MaterialState(order=(0,), names={0: "Si"}))
        new_geometry = LevelSetState(handle=object(), dimension=2, grid_delta=0.5)

        committed = DefaultStateAdapter().commit(state, StateDelta(geometry=new_geometry), _DUMMY_CAPS)

        self.assertIs(committed.geometry, new_geometry)
        self.assertIs(committed.materials, state.materials)
        self.assertEqual(committed.materials.order, (0,))

    def test_commit_materials_delta_requires_the_write_declaration(self) -> None:
        state = _state()
        materials = MaterialState(order=(0, 1), names={0: "Si", 1: "SiO2"})

        with self.assertRaises(ExecutionResolutionError):
            DefaultStateAdapter().commit(state, StateDelta(materials=materials), _DUMMY_CAPS)

        committed = DefaultStateAdapter().commit(state, StateDelta(materials=materials), _MATERIAL_CAPS)
        self.assertIs(committed.materials, materials)
        self.assertIs(committed.geometry, state.geometry)
        self.assertNotEqual(committed.materials.order, state.materials.order)

    def test_commit_rejects_voxel_delta_when_write_is_not_declared(self) -> None:
        state = _state()
        with self.assertRaises(ExecutionResolutionError):
            DefaultStateAdapter().commit(state, StateDelta(voxels=VoxelState(grid=FakeGrid())), _DUMMY_CAPS)

    def test_commit_rejects_geometry_delta_when_write_is_not_declared(self) -> None:
        state = _state()
        capabilities = VariantCapabilities(dimensions=(2,), reads=frozenset({"geometry"}), writes=frozenset())
        with self.assertRaises(ExecutionResolutionError):
            DefaultStateAdapter().commit(state, StateDelta(geometry=state.geometry), capabilities)

    def test_commit_requires_original_geometry_for_voxels(self) -> None:
        with self.assertRaises(StateMissingError):
            DefaultStateAdapter().commit(
                ProcessState(voxels=VoxelState(grid=FakeGrid())),
                StateDelta(voxels=VoxelState(grid=FakeGrid())),
                _VOXEL_CAPS,
            )

    def test_commit_empty_delta_returns_same_state(self) -> None:
        state = _state()
        self.assertIs(DefaultStateAdapter().commit(state, StateDelta(), _DUMMY_CAPS), state)

    def test_commit_fields_delta_returns_new_state(self) -> None:
        state = _state()
        fields = _cellset()
        committed = DefaultStateAdapter().commit(state, StateDelta(fields=fields), _FIELD_CAPS)
        self.assertIs(committed.fields, fields)
        self.assertIsNone(state.fields)
        self.assertIs(committed.geometry, state.geometry)

    def test_commit_layers_delta_returns_new_state(self) -> None:
        state = _state()
        layers = _layers()
        committed = DefaultStateAdapter().commit(state, StateDelta(layers=layers), _LAYER_CAPS)
        self.assertIs(committed.layers, layers)
        self.assertIsNone(state.layers)
        self.assertIs(committed.geometry, state.geometry)
        self.assertIs(committed.materials, state.materials)

    def test_commit_applies_layers_and_fields_independently(self) -> None:
        original_layers = _layers(layer_ids=("oxide",))
        original_fields = _cellset(shape=(4, 3))
        state = dataclasses.replace(_state(), layers=original_layers, fields=original_fields)
        new_layers = _layers(layer_ids=("oxide", "nitride", "metal"))
        new_fields = _cellset(shape=(6, 5), fields={"dose": _field(name="dose", unit="1/cm3", shape=(6, 5))})

        layers_only = DefaultStateAdapter().commit(state, StateDelta(layers=new_layers), _STRUCTURED_CAPS)
        self.assertIs(layers_only.layers, new_layers)
        self.assertIs(layers_only.fields, original_fields)

        fields_only = DefaultStateAdapter().commit(state, StateDelta(fields=new_fields), _STRUCTURED_CAPS)
        self.assertIs(fields_only.fields, new_fields)
        self.assertIs(fields_only.layers, original_layers)

        both = DefaultStateAdapter().commit(
            state,
            StateDelta(layers=new_layers, fields=new_fields),
            _STRUCTURED_CAPS,
        )
        self.assertIs(both.layers, new_layers)
        self.assertIs(both.fields, new_fields)
        # The input state is never mutated.
        self.assertIs(state.layers, original_layers)
        self.assertIs(state.fields, original_fields)

    def test_commit_rejects_layers_delta_when_write_is_not_declared(self) -> None:
        with self.assertRaises(ExecutionResolutionError):
            DefaultStateAdapter().commit(_state(), StateDelta(layers=_layers()), _DUMMY_CAPS)

    def test_commit_rejects_fields_delta_when_write_is_not_declared(self) -> None:
        with self.assertRaises(ExecutionResolutionError):
            DefaultStateAdapter().commit(_state(), StateDelta(fields=_cellset()), _DUMMY_CAPS)


if __name__ == "__main__":
    unittest.main()
