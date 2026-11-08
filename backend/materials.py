"""Persistent, name-stable custom materials for the installed ViennaPS registry."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from process_models.v2.engines import viennaps

from . import storage
from .engine import ENGINE_REGISTRY_VERSION, SUPPORTED_ENGINE_VERSIONS, engine_version

BUILTIN_CATALOG = storage.ROOT / "docs" / "materials-catalog.json"
NATIVE_IDS = storage.ROOT / "docs" / "native-material-ids-4.6.2.json"  # historical inventory; IDs re-verified identical on 4.7.0
CUSTOM_CATALOG = storage.VAR / "custom-materials.json"
CUSTOM_LEGACY_ID_BASE = 177  # Built-ins end at 176 in both 4.6.2 and 4.7.0; first custom is 177.
MAX_CUSTOM_MATERIALS = 64
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_. -]{0,63}$")
# The complete viennaps.MaterialCategory enum exposed by ViennaPS 4.6.2.
MaterialCategory = Literal[
    "Generic", "Silicon", "OxideNitride", "Hardmask", "Metal",
    "Silicide", "Compound", "TwoD", "TCO", "Misc",
]


def _finite_density(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("densityGcm3 must be a JSON number")
    result = float(value)
    if result < 0 or result > 100 or result != result or result in (float("inf"), float("-inf")):
        raise ValueError("densityGcm3 must be finite and between 0 and 100 g/cm³")
    return result


Density = Annotated[float, BeforeValidator(_finite_density)]
HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]


class MaterialMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    color: HexColor
    category: MaterialCategory = "Generic"
    densityGcm3: Density | None = None
    conductive: bool = False
    description: Annotated[str, Field(max_length=500)] = ""


class MaterialDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Annotated[str, Field(min_length=1, max_length=64)]
    id: Annotated[int, Field(ge=CUSTOM_LEGACY_ID_BASE, le=CUSTOM_LEGACY_ID_BASE + MAX_CUSTOM_MATERIALS - 1)]
    customId: Annotated[int, Field(ge=0, lt=MAX_CUSTOM_MATERIALS)]
    color: HexColor
    category: MaterialCategory = "Generic"
    densityGcm3: Density | None = None
    conductive: bool = False
    description: Annotated[str, Field(max_length=500)] = ""


def _custom_record(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"schemaVersion", "engineVersion", "materials"}:
        raise ValueError("custom material registry has an invalid shape")
    if (raw["schemaVersion"] != 1
            or raw["engineVersion"] not in SUPPORTED_ENGINE_VERSIONS
            or not isinstance(raw["materials"], list)):
        raise ValueError("custom material registry header is invalid")
    rows = []
    seen: set[str] = set()
    for index, item in enumerate(raw["materials"]):
        if not isinstance(item, dict) or set(item) - {"name", "id", "customId", "color", "category", "densityGcm3", "conductive", "description", "deleted"}:
            raise ValueError("custom material registry entry has an invalid shape")
        deleted = item.get("deleted", False)
        if not isinstance(deleted, bool):
            raise ValueError("custom material deletion marker must be boolean")
        definition = MaterialDefinition.model_validate({key: value for key, value in item.items() if key != "deleted"})
        row = definition.model_dump(mode="json", exclude_none=True)
        if definition.customId != index or definition.id != CUSTOM_LEGACY_ID_BASE + index:
            raise ValueError("custom material registry IDs are not contiguous and stable")
        if not NAME_RE.fullmatch(definition.name) or definition.name in seen:
            raise ValueError("custom material registry has an invalid or duplicate name")
        seen.add(definition.name)
        if deleted:
            row["deleted"] = True
        rows.append(row)
    if len(rows) > MAX_CUSTOM_MATERIALS:
        raise ValueError("custom material registry exceeds the installed-ID budget")
    return {"schemaVersion": 1, "engineVersion": ENGINE_REGISTRY_VERSION, "materials": rows}


def custom_materials() -> list[dict[str, Any]]:
    try:
        raw = storage.read_json(CUSTOM_CATALOG)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise ValueError("custom material registry cannot be read") from exc
    return _custom_record(raw)["materials"]


def native_builtin_materials() -> list[dict[str, Any]]:
    raw = storage.read_json(NATIVE_IDS)
    rows = raw.get("materials") if raw.get("engineVersion") in SUPPORTED_ENGINE_VERSIONS else None
    if not isinstance(rows, list) or raw.get("maxBuiltInLegacyId") != max((item.get("legacyId", -1) for item in rows), default=-1):
        raise ValueError("installed native material ID inventory is invalid")
    if CUSTOM_LEGACY_ID_BASE != raw["maxBuiltInLegacyId"] + 1:
        raise ValueError("custom material ID range does not follow the installed built-ins")
    return rows


def material_catalog() -> dict[str, Any]:
    catalog = storage.read_json(BUILTIN_CATALOG)
    if catalog.get("schemaVersion") != 1 or catalog.get("engineVersion") not in SUPPORTED_ENGINE_VERSIONS:
        raise ValueError("installed native material catalog header is invalid")
    builtins = []
    names: set[str] = set()
    ids: set[int] = set()
    for item in catalog.get("materials", []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), int) or not isinstance(item.get("name"), str) or not isinstance(item.get("color"), str):
            raise ValueError("installed native material catalog entries are invalid")
        names.add(item["name"])
        ids.add(item["id"])
        builtins.append({**item, "isCustom": False})
    custom = custom_materials()
    native = native_builtin_materials()
    native_names = {item["name"] for item in native}
    native_ids = {int(item["legacyId"]) for item in native}
    if any(item["name"] in native_names or item["name"] in names or item["id"] in ids or item["id"] in native_ids for item in custom):
        raise ValueError("custom material identity collides with the installed built-in catalog")
    visible_custom = [row for row in custom if not row.get("deleted", False)]
    return {"schemaVersion": 1, "engineVersion": engine_version(), "materials": [*builtins, *[{**row, "isCustom": True} for row in visible_custom]]}


def create_material(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) - {"name", "color", "category", "densityGcm3", "conductive", "description"}:
        raise ValueError("material body may contain name, color, category, densityGcm3, conductive and description only")
    try:
        name = payload.get("name")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ValueError("name must start with an ASCII letter and contain only letters, digits, spaces, dots, underscores or hyphens")
        fields = {key: value for key, value in payload.items() if key != "name"}
        fields.setdefault("color", "#8b5cf6")
        fields.setdefault("category", "Generic")
        base = MaterialMetadata.model_validate(fields)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    current = custom_materials()
    native_names = {item["name"].casefold() for item in native_builtin_materials()}
    if name.casefold() in native_names or any(item["name"].casefold() == name.casefold() for item in current):
        raise ValueError("material name already exists; registered identities cannot be reused")
    if len(current) >= MAX_CUSTOM_MATERIALS:
        raise ValueError("the installed native ID range supports at most 64 custom materials")
    index = len(current)
    row = {"name": name, "id": CUSTOM_LEGACY_ID_BASE + index, "customId": index,
           **base.model_dump(mode="json", exclude_none=True)}
    record = {"schemaVersion": 1, "engineVersion": ENGINE_REGISTRY_VERSION, "materials": [*current, row]}
    storage.atomic_json(CUSTOM_CATALOG, record)
    return {**row, "isCustom": True}


def update_material(name: str, payload: Any) -> dict[str, Any]:
    allowed = {"color", "category", "densityGcm3", "conductive", "description"}
    if not isinstance(payload, dict) or not payload or set(payload) - allowed:
        raise ValueError("metadata update requires one or more of color, category, densityGcm3, conductive or description")
    rows = custom_materials()
    index = next((i for i, row in enumerate(rows) if row["name"] == name and not row.get("deleted", False)), None)
    if index is None:
        raise KeyError(name)
    current = rows[index]
    candidate = {key: current.get(key) for key in allowed}
    candidate.update(payload)
    candidate.setdefault("color", "#ffffff")
    try:
        metadata = MaterialMetadata.model_validate(candidate)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    rows[index] = {"name": current["name"], "id": current["id"], "customId": current["customId"],
                   **metadata.model_dump(mode="json", exclude_none=True)}
    storage.atomic_json(CUSTOM_CATALOG, {"schemaVersion": 1, "engineVersion": ENGINE_REGISTRY_VERSION, "materials": rows})
    return {**rows[index], "isCustom": True}


def delete_material(name: str, current_project: Any = None) -> dict[str, Any]:
    rows = custom_materials()
    index = next((i for i, row in enumerate(rows) if row["name"] == name and not row.get("deleted", False)), None)
    if index is None:
        raise KeyError(name)

    references = referenced_material_names(current_project) if current_project is not None else set()
    for path in storage.PROJECTS.glob("*.json"):
        try:
            record = storage.read_json(path)
            saved_project = record["project"]
            if not isinstance(saved_project, dict):
                raise ValueError("saved project has an invalid shape")
            references.update(referenced_material_names(saved_project))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise OSError("saved projects could not be checked; the material was not deleted") from exc
    for path in storage.RUNS.glob("*/manifest.json"):
        try:
            references.update(referenced_material_names(storage.read_json(path)))
        except (OSError, ValueError, TypeError) as exc:
            raise OSError("run history could not be checked; the material was not deleted") from exc

    if name in references:
        raise ValueError(f"{name!r} is used by the current project, a saved project, or run history and cannot be deleted")

    rows[index] = {**rows[index], "deleted": True}
    storage.atomic_json(CUSTOM_CATALOG, {"schemaVersion": 1, "engineVersion": ENGINE_REGISTRY_VERSION, "materials": rows})
    return {**rows[index], "isCustom": True}


GEO_MASK_MATERIAL_DEFAULT = viennaps.GEO_MASK_MATERIAL_DEFAULT
geometry_mask_material_names = viennaps.geometry_mask_material_names


def referenced_material_names(project: Any) -> set[str]:
    raw = project.model_dump(mode="json", exclude_none=True) if hasattr(project, "model_dump") else project
    found: set[str] = set()

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key in {"material", "substrateMaterial", "maskMaterial", "depositMaterial"} and isinstance(child, str):
                    if child:
                        found.add(child)
                elif child_key == "maskMaterials":
                    # Per-rate-set mask lists are real material references: a
                    # custom material named only there must stay registered.
                    if isinstance(child, list):
                        found.update(str(name) for name in child if isinstance(name, str))
                    elif isinstance(child, str) and child:
                        found.add(child)
                elif child_key == "coverMaterial" and isinstance(child, str) and child and child != "GAS":
                    # DenseCellSet cover cells can use a registered material or
                    # the native GAS sentinel. Keep a custom-only reference
                    # registered, but do not invent a catalog entry for GAS.
                    found.add(child)
                elif child_key in {"materialRates", "stickingProbabilityByMaterial", "byMaterial"} and isinstance(child, dict):
                    found.update(str(name) for name in child)
                else:
                    walk(child, child_key)
        elif isinstance(value, list):
            for child in value:
                walk(child, key)

    walk(raw)
    return found


def attach_and_reconcile_project_materials(project: Any) -> None:
    """Restore embedded custom IDs when importing, then normalize to the persistent registry."""
    if not hasattr(project, "materialDefinitions"):
        return
    existing = custom_materials()
    definitions = getattr(project, "materialDefinitions", None) or []
    definition_rows = [item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump") else item for item in definitions]
    if definition_rows:
        if len(definition_rows) > MAX_CUSTOM_MATERIALS:
            raise ValueError("project custom material definitions exceed the supported 64 materials")
        for index, raw in enumerate(definition_rows):
            definition = MaterialDefinition.model_validate(raw)
            if definition.customId != index or definition.id != CUSTOM_LEGACY_ID_BASE + index:
                raise ValueError("project custom material definitions have unstable IDs or are missing earlier registry entries")
            if index < len(existing):
                current = existing[index]
                if current["name"] != definition.name or current["id"] != definition.id or current["customId"] != definition.customId:
                    raise ValueError(f"custom material identity conflict at native ID {definition.id}: installed registry has {current['name']!r}")
            elif index == len(existing):
                native_names = {row["name"].casefold() for row in native_builtin_materials()}
                if definition.name.casefold() in native_names or any(row["name"].casefold() == definition.name.casefold() for row in material_catalog()["materials"]):
                    raise ValueError(f"custom material {definition.name!r} conflicts with an installed material name")
                existing.append(definition.model_dump(mode="json", exclude_none=True))
            else:
                raise ValueError("project custom material definitions are not a complete registration prefix")
        storage.atomic_json(CUSTOM_CATALOG, {"schemaVersion": 1, "engineVersion": ENGINE_REGISTRY_VERSION, "materials": existing})
    names = referenced_material_names(project)
    catalog_names = {row["name"] for row in material_catalog()["materials"]}
    custom_names = {row["name"] for row in custom_materials() if not row.get("deleted", False)}
    missing = names - catalog_names
    if missing:
        raise ValueError("unknown material names: " + ", ".join(sorted(missing)))
    if names & custom_names:
        project.materialDefinitions = [
            MaterialDefinition.model_validate({key: value for key, value in row.items() if key != "deleted"})
            for row in custom_materials()
        ]
    else:
        project.materialDefinitions = None


def worker_registry_payload() -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if key != "deleted"} for row in custom_materials()]
