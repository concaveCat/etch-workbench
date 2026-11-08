"""Drift test between the v2 output contract schema and the state dataclasses.

:func:`process_models.v2.output_schema.output_contract_schema` describes the
``StepResult``/``OutputSet`` values that variants return. Nothing enforces that
description at runtime, so this test compares each described type against
``dataclasses.fields`` of the matching dataclass in
:mod:`process_models.v2.state`.

Schema field names are camelCase while dataclass attributes are snake_case, so
the comparison goes through an explicit mapping instead of a case conversion
heuristic. Test-only: a failure here means the schema and the dataclasses
disagree, and the schema is not edited by this test.

No server, data directory, or native module import is used.
"""

from __future__ import annotations

import dataclasses
import json
import unittest

from process_models.v2 import state
from process_models.v2.output_schema import output_contract_schema

#: Dataclass attribute name -> schema field name, where they differ.
SNAKE_TO_CAMEL = {
    "layer_id": "layerId",
    "material_id": "materialId",
    "source_step": "sourceStep",
    "schema_version": "schemaVersion",
    "material_ids": "materialIds",
    "grid_delta": "gridDelta",
    "domain_handle": "domainHandle",
}

#: Contract type name -> dataclass the schema describes.
CONTRACT_TYPES = {
    "StepResult": state.StepResult,
    "StateDelta": state.StateDelta,
    "OutputSet": state.OutputSet,
    "MetrologyRecord": state.MetrologyRecord,
    "Artifact": state.Artifact,
    "CellSetState": state.CellSetState,
    "MaterialState": state.MaterialState,
    "LevelSetState": state.LevelSetState,
    "VoxelState": state.VoxelState,
    "Field": state.Field,
    "Layer": state.Layer,
    "LayerState": state.LayerState,
}


def _schema_field_name(attribute: str) -> str:
    """Return the schema spelling of a dataclass attribute name."""
    return SNAKE_TO_CAMEL.get(attribute, attribute)


class OutputSchemaDriftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = output_contract_schema()

    def test_schema_is_json_serializable_without_nan(self) -> None:
        self.assertEqual(json.loads(json.dumps(self.schema, allow_nan=False)), self.schema)

    def test_schema_describes_exactly_the_state_types(self) -> None:
        self.assertEqual(set(self.schema["types"]), set(CONTRACT_TYPES))

    def test_dataclass_fields_match_schema_fields(self) -> None:
        for type_name, dataclass_type in CONTRACT_TYPES.items():
            with self.subTest(type=type_name):
                dataclass_names = {_schema_field_name(item.name) for item in dataclasses.fields(dataclass_type)}
                schema_names = set(self.schema["types"][type_name]["fields"])

                self.assertEqual(
                    sorted(dataclass_names - schema_names),
                    [],
                    f"{type_name}: dataclass fields missing from the schema",
                )
                self.assertEqual(
                    sorted(schema_names - dataclass_names),
                    [],
                    f"{type_name}: schema fields that are not dataclass fields",
                )


if __name__ == "__main__":
    unittest.main()
