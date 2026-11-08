"""P4-4 reusable library objects for the Library UI.

The library is *not* a workflow stage entry point. It holds reusable, named
objects that a user can drop into a flow, a project or an experiment later:

``material``
    A material preset. It references a category that exists in the verified
    native material catalog (``GET /api/materials`` -> ``category``) and one
    concrete material from that catalog, and only stores presentation and role
    metadata. The material's own catalog category must equal ``catalog``, so the
    preset can never describe an engine material that does not exist. It never
    invents an engine material and never copies density/conductivity, so a
    catalog change stays authoritative.

``process``
    A single Step template (``operation``/``model``/rates/duration/...). It is
    validated with the same ``TypeAdapter(StepV7)`` the flow compiler uses, so a
    preset can never describe a step the engine would reject. ``id`` is optional
    because a preset has no identity inside a flow: the insert path allocates a
    fresh authored id (``materialize_process_preset``). Insertion-time generated
    ids stay unique per flow; the preset itself keeps whichever id the author
    stored, if any.

``flow``
    A flow definition/template *reference*: ``name`` plus an optional
    ``definitionId``/``templateId`` and a human-readable parameter summary. The
    internal flow JSON is deliberately not copied into the user form; the
    referenced object stays the single source of truth.

``experiment``
    A re-plannable experiment template reference: ``recipeId``/``recipeVersion``
    plus a design/variant summary. Run state (``runId``/``measurementId``/
    ``status``/...) is rejected outright so a template can never smuggle
    someone else's results into a new plan.

Persisted shape is ``LibraryItemV1``. Category-specific payload rules live in
``validate_library_payload`` and run on the write path only: reading an item
back from SQLite must keep working after a catalog name is retired, otherwise a
library entry would disappear from the UI without anyone deleting it.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, get_args

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from ..materials import MaterialCategory
from ..models import (
    BooleanStepV5,
    DirectionalStepV5,
    MultiParticleStepV7,
    PlanarizeStepV5,
    RemoveMaterialStepV5,
    SpecializedStepV7,
    StepV4,
    StepV7,
    StrictModel,
)
from .store import WorkflowValidation

LibraryCategory = Literal["material", "process", "flow", "experiment"]
LIBRARY_CATEGORIES: tuple[LibraryCategory, ...] = (
    "material",
    "process",
    "flow",
    "experiment",
)

ItemId = Annotated[str, Field(pattern=r"^item_[a-f0-9]{24}$")]
MAX_LIBRARY_ITEMS_PER_CATEGORY = 500
MAX_LIBRARY_ITEM_PAYLOAD_BYTES = 64 * 1024
MAX_LIBRARY_ITEM_NAME = 80
MAX_LIBRARY_ITEM_DESCRIPTION = 2000
MAX_LIBRARY_ITEM_NOTES = 2000
MAX_FLOW_SUMMARY_STEPS = 256
MAX_EXPERIMENT_VARIANTS = 64

IdPattern = re.compile(r"^[A-Za-z0-9_-]{3,96}$")
ColorPattern = re.compile(r"^#[0-9a-fA-F]{6}$")
RolePattern = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,39}$")

# The complete installed material-category enum (ViennaPS 4.6.2).
MATERIAL_CATEGORY_VALUES: frozenset[str] = frozenset(get_args(MaterialCategory))

_STEP_ADAPTER: TypeAdapter = TypeAdapter(StepV7)

# Every field any StepV7 variant may carry. Kept as an explicit union of the
# installed step models so an unknown key is reported by name instead of
# surfacing a nine-branch discriminated-union error.
PROCESS_PAYLOAD_FIELDS: frozenset[str] = frozenset(
    field
    for model in (
        StepV4,
        DirectionalStepV5,
        RemoveMaterialStepV5,
        PlanarizeStepV5,
        BooleanStepV5,
        MultiParticleStepV7,
        SpecializedStepV7,
    )
    for field in model.model_fields
)

MATERIAL_PAYLOAD_FIELDS = frozenset(
    {"catalog", "material", "displayName", "color", "role", "notes"}
)
# ``catalogName`` was an early draft field. It is rejected explicitly so a
# client written against it gets a migration hint instead of a generic
# "unknown field" error.
LEGACY_MATERIAL_PAYLOAD_FIELDS = frozenset({"catalogName"})
FLOW_PAYLOAD_FIELDS = frozenset(
    {"name", "definitionId", "templateId", "steps", "parameters", "notes"}
)
EXPERIMENT_PAYLOAD_FIELDS = frozenset(
    {
        "recipeId",
        "recipeVersion",
        "flowId",
        "metricId",
        "unit",
        "design",
        "variants",
        "notes",
    }
)

# A library experiment template must stay re-plannable. These keys describe a
# run that already happened (or is happening), which is never reusable content.
EXPERIMENT_RUNTIME_FIELDS = frozenset(
    {
        "runId",
        "runIds",
        "measurementId",
        "measurementIds",
        "status",
        "state",
        "progress",
        "results",
        "startedAt",
        "finishedAt",
        "completedAt",
        "attemptId",
        "segmentId",
        "queue",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Any) -> Any:
    """Detach a JSON value and reject anything JSON cannot round-trip."""

    return json.loads(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    )


def payload_size(payload: Any) -> int:
    return len(
        json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    )


def new_item_id() -> str:
    return f"item_{uuid.uuid4().hex[:24]}"


def new_step_id() -> str:
    """Return a fresh authored step id that satisfies the StepV7 pattern."""

    return f"step_{uuid.uuid4().hex[:12]}"


class LibraryItemV1(StrictModel):
    """One reusable library object, independent of any run.

    Only structural rules live here. Category-specific payload validation is
    intentionally a write-path concern (see ``validate_library_payload``) so a
    stored item stays readable even when the world around it changed.
    """

    schemaVersion: Literal[1] = 1
    itemId: ItemId
    category: LibraryCategory
    name: Annotated[str, Field(min_length=1, max_length=MAX_LIBRARY_ITEM_NAME)]
    description: Annotated[str, Field(max_length=MAX_LIBRARY_ITEM_DESCRIPTION)] = ""
    payload: dict[str, Any]
    createdAt: str
    updatedAt: str

    @model_validator(mode="after")
    def item_shape(self) -> "LibraryItemV1":
        if not self.name.strip():
            raise ValueError("library item name must not be blank")
        size = payload_size(self.payload)
        if size > MAX_LIBRARY_ITEM_PAYLOAD_BYTES:
            raise ValueError(
                f"library item payload is {size} bytes; "
                f"the limit is {MAX_LIBRARY_ITEM_PAYLOAD_BYTES}"
            )
        return self


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


def _reject_unknown(payload: Mapping[str, Any], allowed: frozenset[str], category: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise WorkflowValidation(
            f"{category} payload has unknown field(s): {', '.join(unknown)}"
        )


def _text(
    payload: Mapping[str, Any],
    key: str,
    *,
    required: bool = False,
    min_length: int = 1,
    max_length: int = 200,
    pattern: re.Pattern[str] | None = None,
) -> str | None:
    if key not in payload:
        if required:
            raise WorkflowValidation(f"payload requires a non-empty {key!r} string")
        return None
    value = payload[key]
    if not isinstance(value, str):
        raise WorkflowValidation(f"payload field {key!r} must be a string")
    if not (min_length <= len(value) <= max_length):
        raise WorkflowValidation(
            f"payload field {key!r} must be {min_length}-{max_length} characters"
        )
    if pattern is not None and pattern.fullmatch(value) is None:
        raise WorkflowValidation(
            f"payload field {key!r} must match {pattern.pattern}"
        )
    return value


def _positive_int(payload: Mapping[str, Any], key: str) -> int:
    if key not in payload:
        raise WorkflowValidation(f"payload requires {key!r}")
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkflowValidation(f"payload field {key!r} must be an integer")
    if value < 1:
        raise WorkflowValidation(f"payload field {key!r} must be at least 1")
    return value


def _catalog_entries(catalog: Mapping[str, str] | None) -> dict[str, str]:
    """Return ``material name -> catalog category`` for the installed catalog."""

    if catalog is not None:
        return {str(name): str(category) for name, category in catalog.items()}
    from ..materials import material_catalog

    try:
        rows = material_catalog().get("materials") or []
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        raise WorkflowValidation(f"material catalog is unavailable: {exc}") from exc
    return {str(row["name"]): str(row.get("category") or "Generic") for row in rows}


def _validate_material(
    payload: Mapping[str, Any], *, catalog: Mapping[str, str] | None
) -> dict[str, Any]:
    legacy = sorted(set(payload) & LEGACY_MATERIAL_PAYLOAD_FIELDS)
    if legacy:
        raise WorkflowValidation(
            "material payload field 'catalogName' is no longer supported; use "
            "'catalog' (a /api/materials category such as 'OxideNitride') plus "
            "'material' (a catalog material name such as 'SiO2')"
        )
    _reject_unknown(payload, MATERIAL_PAYLOAD_FIELDS, "material")
    category_name = _text(payload, "catalog", required=True, max_length=40)
    material_name = _text(payload, "material", required=True, max_length=80)
    if category_name not in MATERIAL_CATEGORY_VALUES:
        raise WorkflowValidation(
            f"material catalog {category_name!r} is not a ViennaPS MaterialCategory; "
            f"expected one of {', '.join(sorted(MATERIAL_CATEGORY_VALUES))}"
        )
    entries = _catalog_entries(catalog)
    if material_name not in entries:
        folded = {name.casefold(): name for name in entries}
        suggestion = folded.get(material_name.casefold())
        hint = (
            f"; did you mean {suggestion!r}?"
            if suggestion is not None
            else "; the name must match GET /api/materials exactly"
        )
        raise WorkflowValidation(
            f"material {material_name!r} is not in the installed material catalog{hint}"
        )
    actual_category = entries[material_name]
    if actual_category != category_name:
        raise WorkflowValidation(
            f"material {material_name!r} is categorized as {actual_category!r}, "
            f"not catalog {category_name!r}"
        )
    _text(payload, "displayName", max_length=80)
    color = _text(payload, "color", max_length=7)
    if color is not None and ColorPattern.fullmatch(color) is None:
        raise WorkflowValidation("material payload color must be '#rrggbb'")
    _text(payload, "role", max_length=40, pattern=RolePattern)
    _text(payload, "notes", max_length=MAX_LIBRARY_ITEM_NOTES)
    return _canonical(dict(payload))


def _validate_process(
    payload: Mapping[str, Any], *, item_name: str
) -> dict[str, Any]:
    if not payload:
        raise WorkflowValidation("process payload must contain a step template")
    _reject_unknown(payload, PROCESS_PAYLOAD_FIELDS, "process")
    for required in ("model", "operation"):
        if required not in payload:
            raise WorkflowValidation(f"process payload requires {required!r}")
    candidate = dict(payload)
    # A preset is not a flow step yet: fill the authored identity scaffolding so
    # the engine schema can validate the process fields themselves. ``id`` is
    # deliberately not persisted back when the author omitted it.
    candidate.setdefault("id", "preset_step")
    candidate.setdefault("enabled", True)
    candidate.setdefault("name", (item_name or "process preset")[:80] or "process preset")
    try:
        _STEP_ADAPTER.validate_python(candidate)
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_context=False)[0]
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "__root__")
        detail = first.get("msg") or "invalid step"
        where = f" at {location}" if location else ""
        raise WorkflowValidation(
            f"process payload is not a valid StepV7{where}: {detail}"
        ) from exc
    return _canonical(dict(payload))


def _validate_flow(payload: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(payload, FLOW_PAYLOAD_FIELDS, "flow")
    _text(payload, "name", required=True, max_length=MAX_LIBRARY_ITEM_NAME)
    for key in ("definitionId", "templateId"):
        value = _text(payload, key, max_length=96)
        if value is not None and IdPattern.fullmatch(value) is None:
            raise WorkflowValidation(f"flow payload field {key!r} must match {IdPattern.pattern}")
    steps = payload.get("steps")
    if steps is not None:
        if not isinstance(steps, list) or not steps:
            raise WorkflowValidation("flow payload steps must be a non-empty list of summaries")
        if len(steps) > MAX_FLOW_SUMMARY_STEPS:
            raise WorkflowValidation(
                f"flow payload holds {len(steps)} step summaries; the limit is {MAX_FLOW_SUMMARY_STEPS}"
            )
        for index, summary in enumerate(steps):
            if not isinstance(summary, str) or not (1 <= len(summary) <= 200):
                raise WorkflowValidation(
                    f"flow payload steps[{index}] must be a 1-200 character string"
                )
    parameters = payload.get("parameters")
    if parameters is not None:
        if not isinstance(parameters, dict) or len(parameters) > 64:
            raise WorkflowValidation("flow payload parameters must be a mapping of at most 64 entries")
        for key, value in parameters.items():
            if not isinstance(key, str) or not (1 <= len(key) <= 64) or not isinstance(value, str) or len(value) > 200:
                raise WorkflowValidation(
                    "flow payload parameters must map 1-64 character names to 1-200 character notes"
                )
    _text(payload, "notes", max_length=MAX_LIBRARY_ITEM_NOTES)
    if payload.get("definitionId") is None and payload.get("templateId") is None and not steps:
        raise WorkflowValidation(
            "flow payload requires a definitionId, a templateId or a steps summary"
        )
    return _canonical(dict(payload))


def _runtime_fields(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in EXPERIMENT_RUNTIME_FIELDS:
                found.append(f"{path}{key}" if path else str(key))
            found.extend(_runtime_fields(item, f"{path}{key}."))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_runtime_fields(item, f"{path}{index}."))
    return found


def _validate_experiment(payload: Mapping[str, Any]) -> dict[str, Any]:
    _reject_unknown(payload, EXPERIMENT_PAYLOAD_FIELDS, "experiment")
    _text(payload, "recipeId", required=True, max_length=64, pattern=IdPattern)
    _positive_int(payload, "recipeVersion")
    for key in ("flowId", "metricId", "unit"):
        value = _text(payload, key, max_length=64)
        if value is not None and key != "unit" and IdPattern.fullmatch(value) is None:
            raise WorkflowValidation(f"experiment payload field {key!r} must match {IdPattern.pattern}")
    design = payload.get("design")
    if design is not None and not isinstance(design, dict):
        raise WorkflowValidation("experiment payload design must be an object")
    variants = payload.get("variants")
    if variants is not None:
        if not isinstance(variants, list) or not variants:
            raise WorkflowValidation("experiment payload variants must be a non-empty list")
        if len(variants) > MAX_EXPERIMENT_VARIANTS:
            raise WorkflowValidation(
                f"experiment payload holds {len(variants)} variants; the limit is {MAX_EXPERIMENT_VARIANTS}"
            )
        for index, variant in enumerate(variants):
            if not isinstance(variant, (dict, str)):
                raise WorkflowValidation(
                    f"experiment payload variants[{index}] must be an object or a summary string"
                )
    _text(payload, "notes", max_length=MAX_LIBRARY_ITEM_NOTES)
    if design is None and not variants:
        raise WorkflowValidation(
            "experiment payload requires a design or a variants summary"
        )
    runtime = _runtime_fields(payload)
    if runtime:
        raise WorkflowValidation(
            "experiment payload must not carry run state: " + ", ".join(sorted(set(runtime)))
        )
    return _canonical(dict(payload))


def validate_library_payload(
    category: LibraryCategory,
    payload: Any,
    *,
    item_name: str = "",
    catalog: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate and detach one category-specific payload.

    ``catalog`` maps material names to their catalog categories and is
    injectable so callers (and tests) can pin the material catalog without
    touching the on-disk registry. Invalid content raises
    :class:`WorkflowValidation`, which the API maps to HTTP 422.
    """

    if category not in LIBRARY_CATEGORIES:
        raise WorkflowValidation(f"unknown library category {category!r}")
    if not isinstance(payload, dict):
        raise WorkflowValidation("library item payload must be an object")
    size = payload_size(payload)
    if size > MAX_LIBRARY_ITEM_PAYLOAD_BYTES:
        raise WorkflowValidation(
            f"library item payload is {size} bytes; the limit is {MAX_LIBRARY_ITEM_PAYLOAD_BYTES}"
        )
    if category == "material":
        return _validate_material(payload, catalog=catalog)
    if category == "process":
        return _validate_process(payload, item_name=item_name)
    if category == "flow":
        return _validate_flow(payload)
    return _validate_experiment(payload)


# ---------------------------------------------------------------------------
# Item construction
# ---------------------------------------------------------------------------


def create_library_item(
    *,
    category: LibraryCategory,
    name: str,
    description: str = "",
    payload: Any,
    catalog: Mapping[str, str] | None = None,
    now: str | None = None,
) -> LibraryItemV1:
    timestamp = now or _now()
    validated = validate_library_payload(
        category, payload, item_name=name, catalog=catalog
    )
    return LibraryItemV1(
        schemaVersion=1,
        itemId=new_item_id(),
        category=category,
        name=name,
        description=description,
        payload=validated,
        createdAt=timestamp,
        updatedAt=timestamp,
    )


def update_library_item(
    existing: LibraryItemV1 | Mapping[str, Any],
    *,
    name: str,
    description: str,
    payload: Any,
    catalog: Mapping[str, str] | None = None,
    now: str | None = None,
) -> LibraryItemV1:
    """Return the updated item; identity and category never change."""

    if not isinstance(existing, LibraryItemV1):
        existing = LibraryItemV1.model_validate(existing)
    validated = validate_library_payload(
        existing.category, payload, item_name=name, catalog=catalog
    )
    return LibraryItemV1(
        schemaVersion=1,
        itemId=existing.itemId,
        category=existing.category,
        name=name,
        description=description,
        payload=validated,
        createdAt=existing.createdAt,
        updatedAt=now or _now(),
    )


def materialize_process_preset(
    payload: Mapping[str, Any],
    *,
    step_id: str | None = None,
    name: str | None = None,
    item_name: str = "",
) -> dict[str, Any]:
    """Turn a process preset into one insertable StepV7 document.

    A preset has no identity inside a flow, so the insert path always allocates
    a fresh authored id unless the caller supplies one. The result has been
    validated by the same ``TypeAdapter(StepV7)`` as the flow compiler.
    """

    validated = _validate_process(payload, item_name=item_name)
    document = dict(validated)
    document["id"] = step_id or new_step_id()
    document.setdefault("enabled", True)
    if name is not None:
        document["name"] = name
    else:
        document.setdefault("name", (item_name or "process preset")[:80] or "process preset")
    try:
        step = _STEP_ADAPTER.validate_python(document)
    except ValidationError as exc:
        raise WorkflowValidation(f"process preset cannot be inserted as a StepV7: {exc}") from exc
    return step.model_dump(mode="json", exclude_none=True)
