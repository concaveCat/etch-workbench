"""Material-name collection and early registry validation."""

from __future__ import annotations

import unittest

from pydantic import BaseModel

from process_models.v2.materials import (
    StaticMaterialRegistry,
    collect_material_names,
    missing_material_names,
)


class _Nested(BaseModel):
    material: str = "Si"
    maskMaterials: list[str] = []
    diffusionMaterials: list[str] = []
    note: str = "not-a-material"


class _Params(BaseModel):
    material: str = "Si"
    screenMaterials: list[str] = []
    maskMaterial: str = ""
    nested: _Nested = _Nested()
    materialRates: dict[str, dict[str, float]] = {}


class MaterialCollectionTest(unittest.TestCase):
    def test_collects_material_fields_and_skips_empty_values(self) -> None:
        params = _Params(
            screenMaterials=["SiO2"],
            nested=_Nested(material="SiGe", maskMaterials=["Mask"], diffusionMaterials=["Si"]),
            materialRates={"Si": {"x": 1.0}},
        )
        self.assertEqual(
            collect_material_names(params),
            ("Si", "SiO2", "SiGe", "Mask"),
        )

    def test_missing_names_are_reported(self) -> None:
        registry = StaticMaterialRegistry({"Si": 10, "SiO2": 30})
        params = _Params(screenMaterials=["SiO2", "Unknown"], nested=_Nested(material="SiGe"))
        self.assertEqual(missing_material_names(params, registry), ("Unknown", "SiGe"))

    def test_plain_mapping_is_supported(self) -> None:
        self.assertEqual(
            collect_material_names({"material": "Si", "blockingMaterials": ["SiO2"]}),
            ("Si", "SiO2"),
        )


if __name__ == "__main__":
    unittest.main()
