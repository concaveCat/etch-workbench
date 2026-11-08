"""P4-3 local team library for modules, definitions, templates and experiment templates.

The library is deliberately local and single-user. A bundle is a content-addressed
snapshot that can be written to disk, copied to another workstation and imported
with an explicit conflict policy. It never carries run, segment or measurement
state, so an experiment template can be re-planned instead of replaying someone
else's results.

Conflict semantics are intentionally small and predictable:

* ``reject`` keeps every identity in the bundle. Existing identical content is
  skipped; any identity that already holds different content raises a conflict.
* ``clone`` always allocates fresh definition/template/experiment identities.
  Module revisions are never renumbered or rewritten because definitions pin
  ``(moduleId, revision)``; a module conflict therefore requires identical
  content in both policies (explicitly rejected otherwise).
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from ..models import ProjectV7, StrictModel
from .experiments import _aggregate, _queue_summary
from .ids import sha256_hex
from .modules import (
    FlowDefinitionV2,
    FlowNodeModuleCallV1,
    FlowNodeRepeatV1,
    ModuleV1,
)
from .store import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowStore,
    WorkflowValidation,
)
from .templates import FlowTemplateV1

MAX_LIBRARY_MODULES = 100
MAX_LIBRARY_MODULE_ENTRIES = 300
MAX_LIBRARY_DEFINITIONS = 100
MAX_LIBRARY_TEMPLATES = 200
MAX_LIBRARY_EXPERIMENT_TEMPLATES = 200
MAX_LIBRARY_VARIANTS = 256
MAX_LIBRARY_REPLICATES = 64

BundleId = Annotated[str, Field(pattern=r"^lib_[a-f0-9]{24}$")]
Identifier = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
TemplateId = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
RecipeId = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
ExperimentId = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Any) -> Any:
    """Detach a JSON value so callers cannot mutate stored/imported content."""

    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


class LibraryModuleV1(StrictModel):
    """One exact retained module revision."""

    module: ModuleV1
    revision: Annotated[int, Field(ge=1)]


class LibraryReplicateV1(StrictModel):
    """A reusable replicate condition. Runtime fields are intentionally absent."""

    seed: int


class LibraryExperimentVariantV1(StrictModel):
    parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    replicates: Annotated[
        list[LibraryReplicateV1], Field(min_length=1, max_length=MAX_LIBRARY_REPLICATES)
    ]


class LibraryExperimentTemplateV1(StrictModel):
    """A re-plannable experiment: conditions and seeds only, never results."""

    name: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(max_length=2000)] = ""
    project: ProjectV7
    recipeId: RecipeId
    recipeVersion: Annotated[int, Field(ge=1)]
    design: dict[str, Any] | None = None
    kind: Literal["manual", "doe"]
    variants: Annotated[
        list[LibraryExperimentVariantV1], Field(min_length=1, max_length=MAX_LIBRARY_VARIANTS)
    ]


class LibraryBundleV1(StrictModel):
    """A portable, content-addressed snapshot of the local team library."""

    schemaVersion: Literal[1] = 1
    bundleId: BundleId
    name: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(max_length=2000)] = ""
    createdAt: str
    modules: Annotated[
        list[LibraryModuleV1], Field(max_length=MAX_LIBRARY_MODULE_ENTRIES)
    ] = Field(default_factory=list)
    definitions: Annotated[
        list[FlowDefinitionV2], Field(max_length=MAX_LIBRARY_DEFINITIONS)
    ] = Field(default_factory=list)
    templates: Annotated[
        list[FlowTemplateV1], Field(max_length=MAX_LIBRARY_TEMPLATES)
    ] = Field(default_factory=list)
    experimentTemplates: Annotated[
        list[LibraryExperimentTemplateV1],
        Field(max_length=MAX_LIBRARY_EXPERIMENT_TEMPLATES),
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bundle(self) -> "LibraryBundleV1":
        module_keys = [(entry.module.moduleId, entry.revision) for entry in self.modules]
        if len(module_keys) != len(set(module_keys)):
            raise ValueError("bundle repeats a (moduleId, revision) pair")
        if len({module_id for module_id, _revision in module_keys}) > MAX_LIBRARY_MODULES:
            raise ValueError(f"bundle exceeds the {MAX_LIBRARY_MODULES} module limit")
        definition_ids = [definition.definitionId for definition in self.definitions]
        if len(definition_ids) != len(set(definition_ids)):
            raise ValueError("bundle repeats a definitionId")
        template_ids = [template.templateId for template in self.templates]
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("bundle repeats a templateId")
        expected = _bundle_id_for(
            name=self.name,
            description=self.description,
            modules=self.modules,
            definitions=self.definitions,
            templates=self.templates,
            experimentTemplates=self.experimentTemplates,
        )
        if self.bundleId != expected:
            raise ValueError("bundleId does not match the bundle content hash")
        return self


class LibraryExportRequestV1(StrictModel):
    """Selectors omitted (``None``) export every local object of that kind."""

    name: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(max_length=2000)] = ""
    moduleIds: list[Identifier] | None = None
    definitionIds: list[Identifier] | None = None
    templateIds: list[TemplateId] | None = None
    experimentIds: list[ExperimentId] | None = None


class LibraryModuleImportV1(StrictModel):
    moduleId: str
    revision: Annotated[int, Field(ge=1)]
    status: Literal["imported", "existing"]


class LibraryRenameV1(StrictModel):
    kind: Literal["definition", "template"]
    previousId: str
    importedId: str


class LibraryImportResultV1(StrictModel):
    schemaVersion: Literal[1] = 1
    bundleId: BundleId
    policy: Literal["reject", "clone"]
    modules: list[LibraryModuleImportV1] = Field(default_factory=list)
    definitionIds: list[str] = Field(default_factory=list)
    templateIds: list[str] = Field(default_factory=list)
    experimentIds: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    renames: list[LibraryRenameV1] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Content identity
# ---------------------------------------------------------------------------


def _dump_module(entry: LibraryModuleV1) -> dict[str, Any]:
    return {
        "module": entry.module.model_dump(mode="json", exclude_none=True),
        "revision": entry.revision,
    }


def _experiment_template_key(template: LibraryExperimentTemplateV1) -> str:
    """Canonical ordering/identity key for a run-state-free experiment template.

    Experiment templates have no id of their own, so their content hash is also
    the bundle-local identity. Using it for ordering keeps the serialized bundle
    canonical no matter how the source store listed its experiments, and keeps
    reject imports idempotent when a bundle is re-serialized in another order.
    """

    return sha256_hex(template.model_dump(mode="json", exclude_none=True))


def _bundle_id_for(
    *,
    name: str,
    description: str,
    modules: list[LibraryModuleV1],
    definitions: list[FlowDefinitionV2],
    templates: list[FlowTemplateV1],
    experimentTemplates: list[LibraryExperimentTemplateV1],
) -> str:
    module_rows = [_dump_module(entry) for entry in modules]
    module_rows.sort(key=lambda row: (row["module"]["moduleId"], row["revision"]))
    definition_rows = [
        definition.model_dump(mode="json", exclude_none=True)
        for definition in definitions
    ]
    definition_rows.sort(key=lambda row: row["definitionId"])
    template_rows = [
        template.model_dump(mode="json", exclude_none=True) for template in templates
    ]
    template_rows.sort(key=lambda row: row["templateId"])
    experiment_rows = [
        template.model_dump(mode="json", exclude_none=True)
        for template in experimentTemplates
    ]
    experiment_rows.sort(key=sha256_hex)
    payload = {
        "schemaVersion": 1,
        "name": name,
        "description": description,
        "modules": module_rows,
        "definitions": definition_rows,
        "templates": template_rows,
        "experimentTemplates": experiment_rows,
    }
    return f"lib_{sha256_hex(payload)[:24]}"


def library_bundle_id(bundle: LibraryBundleV1) -> str:
    """Return the stable bundle id for an already validated bundle."""

    return _bundle_id_for(
        name=bundle.name,
        description=bundle.description,
        modules=bundle.modules,
        definitions=bundle.definitions,
        templates=bundle.templates,
        experimentTemplates=bundle.experimentTemplates,
    )


# ---------------------------------------------------------------------------
# Referenced module discovery
# ---------------------------------------------------------------------------


def _definition_module_refs(definition: FlowDefinitionV2) -> list[tuple[str, int | None]]:
    refs: list[tuple[str, int | None]] = []

    def walk(nodes: Any) -> None:
        for node in nodes:
            if isinstance(node, FlowNodeModuleCallV1):
                refs.append((node.moduleId, node.moduleRevision))
            elif isinstance(node, FlowNodeRepeatV1):
                walk(node.body)

    walk(definition.nodes)
    return refs


def _module_refs(module: ModuleV1) -> list[tuple[str, int | None]]:
    refs: list[tuple[str, int | None]] = []
    for step in module.steps:
        if "moduleId" not in step:
            continue
        revision = step.get("moduleRevision")
        refs.append((str(step["moduleId"]), None if revision is None else int(revision)))
    return refs


def _unique_requested(values: list[str] | None, label: str) -> list[str] | None:
    if values is None:
        return None
    if len(values) != len(set(values)):
        raise WorkflowValidation(f"duplicate {label} in export request")
    return values


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _experiment_template(document: dict[str, Any]) -> LibraryExperimentTemplateV1:
    """Convert a stored experiment document into a run-state-free template."""

    variants: list[dict[str, Any]] = []
    for variant in document.get("variants") or []:
        replicates = []
        for replicate in variant.get("replicates") or []:
            if "seed" not in replicate:
                raise WorkflowValidation(
                    f"experiment {document.get('experimentId')!r} has a replicate without a seed"
                )
            replicates.append({"seed": int(replicate["seed"])})
        if not replicates:
            raise WorkflowValidation(
                f"experiment {document.get('experimentId')!r} has a variant without replicates"
            )
        variants.append(
            {
                "parameters": _canonical(variant.get("parameters") or {}),
                "replicates": replicates,
            }
        )
    if not variants:
        raise WorkflowValidation(
            f"experiment {document.get('experimentId')!r} has no variants to export"
        )
    kind = str(document.get("kind") or "manual")
    if kind not in {"manual", "doe"}:
        raise WorkflowValidation(f"experiment {document.get('experimentId')!r} has unsupported kind {kind!r}")
    payload = {
        "name": str(document.get("name") or document.get("experimentId")),
        "description": str(document.get("description") or ""),
        "project": document.get("project"),
        "recipeId": document.get("recipeId"),
        "recipeVersion": int(document.get("recipeVersion") or 1),
        "design": _canonical(document["design"]) if document.get("design") is not None else None,
        "kind": kind,
        "variants": variants,
    }
    try:
        return LibraryExperimentTemplateV1.model_validate(payload)
    except ValueError as exc:
        raise WorkflowValidation(
            f"experiment {document.get('experimentId')!r} cannot be exported as a template: {exc}"
        ) from exc


def build_bundle(
    store: WorkflowStore,
    request: LibraryExportRequestV1,
    *,
    created_at: str | None = None,
) -> LibraryBundleV1:
    """Collect the requested objects plus every module revision they pin."""

    module_ids = _unique_requested(request.moduleIds, "moduleId")
    definition_ids = _unique_requested(request.definitionIds, "definitionId")
    template_ids = _unique_requested(request.templateIds, "templateId")
    experiment_ids = _unique_requested(request.experimentIds, "experimentId")

    if module_ids is None:
        selected_module_ids = sorted(row["moduleId"] for row in store.list_modules())
    else:
        selected_module_ids = list(module_ids)
        for module_id in selected_module_ids:
            store.get_module(module_id)

    if definition_ids is None:
        selected_definition_ids = sorted(
            row["definitionId"] for row in store.list_flow_definitions()
        )
    else:
        selected_definition_ids = list(definition_ids)

    definitions: list[FlowDefinitionV2] = []
    for definition_id in selected_definition_ids:
        definitions.append(store.get_flow_definition(definition_id))

    if template_ids is None:
        selected_template_ids = sorted(row["templateId"] for row in store.list_templates())
    else:
        selected_template_ids = list(template_ids)
    templates = [store.get_template(template_id) for template_id in selected_template_ids]

    if experiment_ids is None:
        experiment_documents = store.list_experiment_documents()
    else:
        experiment_documents = [store.get_experiment(experiment_id) for experiment_id in experiment_ids]
    if len(experiment_documents) > MAX_LIBRARY_EXPERIMENT_TEMPLATES:
        raise WorkflowValidation(
            f"experiment export exceeds the {MAX_LIBRARY_EXPERIMENT_TEMPLATES} template limit"
        )
    experiment_templates = [_experiment_template(document) for document in experiment_documents]
    # Canonical order so the same content always serializes to the same bundle,
    # regardless of how the source store ordered its experiment listing.
    experiment_templates.sort(key=_experiment_template_key)

    wanted: dict[str, set[int]] = defaultdict(set)
    pending: list[tuple[str, int]] = []
    current_ids: set[str] = set()

    def require(module_id: str, revision: int) -> None:
        if revision < 1:
            raise WorkflowValidation(f"module {module_id!r} has an invalid revision {revision}")
        revisions = wanted[module_id]
        if revision not in revisions:
            revisions.add(revision)
            pending.append((module_id, revision))

    def require_current(module_id: str) -> None:
        if module_id in current_ids:
            return
        revision = store.current_module_revision(module_id)
        current_ids.add(module_id)
        require(module_id, revision)

    for module_id in selected_module_ids:
        require_current(module_id)
    for definition in definitions:
        for module_id, revision in _definition_module_refs(definition):
            if revision is None:
                require_current(module_id)
            else:
                require(module_id, revision)
                # The bundle also carries the current revision so a recipient
                # can keep using unpinned calls without silently changing them.
                require_current(module_id)

    while pending:
        module_id, revision = pending.pop()
        module = store.get_module(module_id, revision)
        for nested_id, nested_revision in _module_refs(module):
            if nested_revision is None:
                require_current(nested_id)
            else:
                require(nested_id, nested_revision)

    if len(wanted) > MAX_LIBRARY_MODULES:
        raise WorkflowValidation(f"module export exceeds the {MAX_LIBRARY_MODULES} module limit")
    entries = sum(len(revisions) for revisions in wanted.values())
    if entries > MAX_LIBRARY_MODULE_ENTRIES:
        raise WorkflowValidation(
            f"module export exceeds the {MAX_LIBRARY_MODULE_ENTRIES} revision entry limit"
        )
    if len(definitions) > MAX_LIBRARY_DEFINITIONS:
        raise WorkflowValidation(
            f"definition export exceeds the {MAX_LIBRARY_DEFINITIONS} definition limit"
        )
    if len(templates) > MAX_LIBRARY_TEMPLATES:
        raise WorkflowValidation(
            f"template export exceeds the {MAX_LIBRARY_TEMPLATES} template limit"
        )

    modules = [
        LibraryModuleV1(module=store.get_module(module_id, revision), revision=revision)
        for module_id in sorted(wanted)
        for revision in sorted(wanted[module_id])
    ]

    created = created_at or _now()
    bundle_id = _bundle_id_for(
        name=request.name,
        description=request.description,
        modules=modules,
        definitions=definitions,
        templates=templates,
        experimentTemplates=experiment_templates,
    )
    return LibraryBundleV1(
        schemaVersion=1,
        bundleId=bundle_id,
        name=request.name,
        description=request.description,
        createdAt=created,
        modules=modules,
        definitions=definitions,
        templates=templates,
        experimentTemplates=experiment_templates,
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _document_digest(document: Any) -> str:
    return sha256_hex(document.model_dump(mode="json", exclude_none=True))


def _suffixed_id(base: str, prefix: str, *, max_length: int = 64) -> str:
    suffix = uuid.uuid4().hex[:8]
    head = base[: max_length - len(suffix) - 1]
    candidate = f"{head}_{suffix}"
    if not candidate or not candidate[0].isalpha():
        candidate = f"{prefix}_{suffix}"
    return candidate


def _existing_definition(store: WorkflowStore, definition_id: str) -> FlowDefinitionV2 | None:
    try:
        return store.get_flow_definition(definition_id)
    except WorkflowNotFound:
        return None


def _existing_template(store: WorkflowStore, template_id: str) -> FlowTemplateV1 | None:
    try:
        return store.get_template(template_id)
    except WorkflowNotFound:
        return None


def _module_exists(store: WorkflowStore, module_id: str) -> bool:
    try:
        store.current_module_revision(module_id)
        return True
    except WorkflowNotFound:
        return False


def _bundle_current_revision(
    module_groups: Mapping[str, Mapping[int, ModuleV1]], module_id: str
) -> int | None:
    """Return the revision an unpinned call would have resolved to at export."""

    revisions = module_groups.get(module_id)
    return max(revisions) if revisions else None


def _effective_current_revision(
    store: WorkflowStore,
    module_groups: Mapping[str, Mapping[int, ModuleV1]],
    module_id: str,
) -> int | None:
    """Return the current revision the target will use after this import.

    Import never downgrades a newer local current revision, so an unpinned call
    resolves to ``max(local_current, bundle_current)``.
    """

    bundle_revision = _bundle_current_revision(module_groups, module_id)
    try:
        local_revision = store.current_module_revision(module_id)
    except WorkflowNotFound:
        local_revision = None
    if bundle_revision is None:
        return local_revision
    if local_revision is None:
        return bundle_revision
    return max(local_revision, bundle_revision)


def _experiment_source_index(
    store: WorkflowStore, bundle_id: str
) -> tuple[dict[str, str], dict[int, str]]:
    """Index experiments already imported from one bundle.

    The template content hash is authoritative. ``templateIndex`` is kept as a
    fallback only for documents written before the hash was recorded: a bundle
    may be re-serialized with its experiment templates in another order while
    keeping the same content-addressed ``bundleId``.
    """

    by_hash: dict[str, str] = {}
    by_index: dict[int, str] = {}
    for document in store.list_experiment_documents():
        source = document.get("librarySource") or {}
        if source.get("bundleId") != bundle_id:
            continue
        experiment_id = str(document.get("experimentId"))
        template_hash = source.get("templateHash")
        if isinstance(template_hash, str):
            by_hash[template_hash] = experiment_id
            continue
        index = source.get("templateIndex")
        if isinstance(index, int):
            by_index.setdefault(index, experiment_id)
    return by_hash, by_index


def _new_experiment_document(
    template: LibraryExperimentTemplateV1,
    *,
    bundle_id: str,
    index: int,
    template_hash: str,
    recipe: Any,
    metric: Any,
    now: str,
) -> dict[str, Any]:
    variants: list[dict[str, Any]] = []
    for ordinal, variant in enumerate(template.variants, start=1):
        replicates = [
            {
                "replicateId": f"rep_{uuid.uuid4().hex[:12]}",
                "seed": int(replicate.seed),
                "status": "planned",
                "statisticalSample": False,
                "attemptCount": 0,
            }
            for replicate in variant.replicates
        ]
        variants.append(
            {
                "variantId": f"var_{uuid.uuid4().hex[:12]}",
                "ordinal": ordinal,
                "parameters": _canonical(variant.parameters),
                "replicates": replicates,
            }
        )
    # Derive the flow id from the template content, not its position, so the
    # same template produces the same flow id on every workstation.
    flow_id = f"flow_lib_{sha256_hex([bundle_id, template_hash])[:12]}"
    document: dict[str, Any] = {
        "schemaVersion": 2,
        "kind": template.kind,
        "experimentId": f"exp_{uuid.uuid4().hex[:16]}",
        "name": template.name,
        "description": template.description,
        "flowId": flow_id,
        "project": template.project.model_dump(mode="json", exclude_none=True),
        "recipeId": recipe.recipeId,
        "recipeVersion": recipe.version,
        "metricId": metric.metricId,
        "unit": metric.unit,
        "maxAttempts": 1,
        "costPerRunUsd": 0.0,
        "variants": variants,
        "status": "planned",
        "createdAt": now,
        "updatedAt": now,
        "baseCompileId": None,
        "bindingId": None,
        "binding": None,
        "retention": {"mode": "keep-all", "updatedAt": now},
        "librarySource": {
            "bundleId": bundle_id,
            "templateName": template.name,
            "templateIndex": index,
            "templateHash": template_hash,
        },
    }
    if template.design is not None:
        document["design"] = _canonical(template.design)
    document["queue"] = _queue_summary(document)
    document["aggregation"] = _aggregate(document)
    return document


def _validate_import_bundle(bundle: LibraryBundleV1) -> None:
    # The model validator already enforces identity, size and uniqueness. This
    # helper documents that import trusts only a validated, self-consistent bundle.
    if bundle.schemaVersion != 1:
        raise WorkflowValidation("unsupported library bundle schema version")


def import_bundle(
    store: WorkflowStore,
    bundle: LibraryBundleV1 | dict[str, Any],
    policy: Literal["reject", "clone"] = "reject",
) -> LibraryImportResultV1:
    """Import one bundle under an explicit conflict policy.

    Validation and conflict detection happen before the first write so a failed
    import does not leave modules behind without their definitions.
    """

    if policy not in {"reject", "clone"}:
        raise WorkflowValidation("library import policy must be 'reject' or 'clone'")
    if not isinstance(bundle, LibraryBundleV1):
        bundle = LibraryBundleV1.model_validate(bundle)
    _validate_import_bundle(bundle)

    now = _now()
    module_groups: dict[str, dict[int, ModuleV1]] = defaultdict(dict)
    for entry in bundle.modules:
        module_groups[entry.module.moduleId][entry.revision] = entry.module

    conflicts: list[str] = []

    # --- module preflight -------------------------------------------------
    for module_id in sorted(module_groups):
        revisions = module_groups[module_id]
        target_revision = max(revisions)
        local_current = None
        try:
            local_current = store.current_module_revision(module_id)
            local_document = store.get_module(module_id)
        except WorkflowNotFound:
            local_document = None
        for revision, module in revisions.items():
            try:
                existing = store.get_module(module_id, revision)
            except WorkflowNotFound:
                continue
            if _document_digest(existing) != _document_digest(module):
                conflicts.append(f"module:{module_id}@{revision}")
        if local_document is not None:
            if local_current is not None and local_current == target_revision:
                if _document_digest(local_document) != _document_digest(revisions[target_revision]):
                    # Re-import must be byte-identical; a bundle must never
                    # silently overwrite content already retained locally.
                    conflicts.append(f"module:{module_id}@current:{local_current}")
            # A locally newer current revision is never downgraded. Importing an
            # older bundle only reuses the exact revisions it carries.

    def pinned_revision_available(module_id: str, revision: int) -> bool:
        return revision in module_groups.get(module_id, {}) or store.module_revision_exists(
            module_id, revision
        )

    def module_available(module_id: str) -> bool:
        return bool(module_groups.get(module_id)) or _module_exists(store, module_id)

    for module in (entry for entries in module_groups.values() for entry in entries.values()):
        for nested_id, nested_revision in _module_refs(module):
            if nested_revision is None:
                if not module_available(nested_id):
                    raise WorkflowValidation(
                        f"module {module.moduleId!r} calls module {nested_id!r}, "
                        "which is neither in the bundle nor retained locally"
                    )
            elif not pinned_revision_available(nested_id, nested_revision):
                raise WorkflowValidation(
                    f"module {module.moduleId!r} pins {nested_id!r} revision "
                    f"{nested_revision}, which is neither in the bundle nor retained locally"
                )

    # --- definition preflight --------------------------------------------
    definition_plans: list[tuple[FlowDefinitionV2, str, str | None]] = []
    for definition in bundle.definitions:
        for module_id, revision in _definition_module_refs(definition):
            if revision is None:
                if not module_available(module_id):
                    raise WorkflowValidation(
                        f"flow definition {definition.definitionId!r} calls module "
                        f"{module_id!r}, which is neither in the bundle nor retained locally"
                    )
            elif not pinned_revision_available(module_id, revision):
                raise WorkflowValidation(
                    f"flow definition {definition.definitionId!r} pins {module_id!r} "
                    f"revision {revision}, which is neither in the bundle nor retained locally"
                )
        existing = _existing_definition(store, definition.definitionId)
        if policy == "reject":
            if existing is None:
                definition_plans.append((definition, definition.definitionId, None))
            elif _document_digest(existing) == _document_digest(definition):
                definition_plans.append((definition, definition.definitionId, "skipped"))
            else:
                conflicts.append(f"definition:{definition.definitionId}")
        else:
            new_id = _suffixed_id(definition.definitionId, "def")
            while _existing_definition(store, new_id) is not None:
                new_id = _suffixed_id(definition.definitionId, "def")
            definition_plans.append((definition, new_id, None))

    # --- template preflight ----------------------------------------------
    template_plans: list[tuple[FlowTemplateV1, str, str | None]] = []
    for template in bundle.templates:
        existing = _existing_template(store, template.templateId)
        if policy == "reject":
            if existing is None:
                template_plans.append((template, template.templateId, None))
            elif _document_digest(existing) == _document_digest(template):
                template_plans.append((template, template.templateId, "skipped"))
            else:
                conflicts.append(f"template:{template.templateId}")
        else:
            new_id = _suffixed_id(template.templateId, "tpl")
            while _existing_template(store, new_id) is not None:
                new_id = _suffixed_id(template.templateId, "tpl")
            template_plans.append((template, new_id, None))

    # --- experiment preflight --------------------------------------------
    imported_by_hash, imported_by_index = _experiment_source_index(store, bundle.bundleId)
    experiment_plans: list[
        tuple[LibraryExperimentTemplateV1, int, str, Any, Any]
    ] = []
    experiment_skips: list[tuple[int, str]] = []
    for index, template in enumerate(bundle.experimentTemplates):
        try:
            recipe = store.get_recipe(template.recipeId, template.recipeVersion)
        except WorkflowNotFound as exc:
            raise WorkflowConflict(
                f"experiment template {template.name!r} requires recipe "
                f"{template.recipeId} v{template.recipeVersion}, which does not exist"
            ) from exc
        try:
            metric = store.get_metric(recipe.metricId)
        except WorkflowNotFound as exc:
            raise WorkflowConflict(
                f"experiment template {template.name!r} requires metric "
                f"{recipe.metricId!r}, which does not exist"
            ) from exc
        template_hash = _experiment_template_key(template)
        if policy == "reject":
            imported_experiment_id = imported_by_hash.get(template_hash)
            if imported_experiment_id is None:
                imported_experiment_id = imported_by_index.get(index)
            if imported_experiment_id is not None:
                experiment_skips.append((index, imported_experiment_id))
                continue
        experiment_plans.append((template, index, template_hash, recipe, metric))

    # --- unpinned resolution preflight -----------------------------------
    # A definition or module that calls another module without an explicit
    # revision resolves against the target's *current* revision. Export adds the
    # exporter's current revision to the bundle, but import never downgrades a
    # newer local current. Without this check the same definition would silently
    # compile different steps on the recipient.
    #
    # The walk is over every definition and every module revision in the bundle,
    # then follows the modules they reach (including locally retained modules
    # that the bundle itself references). It must not depend on whether the
    # definition/module already exists or is skipped: an unchanged parent module
    # can still contain an unpinned call whose child drifts on import.
    visited_modules: set[tuple[str, int]] = set()

    def walk_module_refs(owner: str, refs: list[tuple[str, int | None]]) -> None:
        for module_id, revision in refs:
            if revision is None:
                expected = _bundle_current_revision(module_groups, module_id)
                effective = _effective_current_revision(store, module_groups, module_id)
                if expected is not None and effective != expected:
                    conflicts.append(
                        f"{owner}:unpinned:{module_id}@{expected}->{effective}"
                    )
                target_revision = effective
            else:
                # An explicit pin is the author's contract; never compare it
                # against the target's current revision.
                target_revision = revision
            if target_revision is None:
                continue
            key = (module_id, target_revision)
            if key in visited_modules:
                continue
            visited_modules.add(key)
            module = module_groups.get(module_id, {}).get(target_revision)
            if module is None:
                try:
                    module = store.get_module(module_id, target_revision)
                except WorkflowNotFound:
                    continue
            walk_module_refs(f"module:{module_id}", _module_refs(module))

    for definition, _target_id, _skipped in definition_plans:
        walk_module_refs(
            f"definition:{definition.definitionId}",
            _definition_module_refs(definition),
        )
    for module_id, revisions in module_groups.items():
        for module in revisions.values():
            walk_module_refs(f"module:{module_id}", _module_refs(module))

    if conflicts:
        raise WorkflowConflict(
            "library import conflicts with local content: " + ", ".join(sorted(set(conflicts)))
        )

    # --- writes -----------------------------------------------------------
    result = LibraryImportResultV1(
        bundleId=bundle.bundleId,
        policy=policy,
        skipped=[],
        conflicts=[],
    )
    for module_id in sorted(module_groups):
        revisions = module_groups[module_id]
        target_revision = max(revisions)
        for revision in sorted(revisions):
            status = store.import_module_revision(
                revisions[revision],
                revision,
                make_current=revision == target_revision,
            )
            result.modules.append(
                LibraryModuleImportV1(
                    moduleId=module_id,
                    revision=revision,
                    status=status,
                )
            )

    for definition, target_id, skipped in definition_plans:
        if skipped:
            result.skipped.append(f"definition:{definition.definitionId}")
            continue
        document = definition
        if policy == "clone":
            document = definition.model_copy(
                update={"definitionId": target_id, "revision": 1}
            )
        if target_id != definition.definitionId:
            result.renames.append(
                LibraryRenameV1(
                    kind="definition",
                    previousId=definition.definitionId,
                    importedId=target_id,
                )
            )
        store.save_flow_definition(document)
        result.definitionIds.append(target_id)

    for template, target_id, skipped in template_plans:
        if skipped:
            result.skipped.append(f"template:{template.templateId}")
            continue
        document = template
        if policy == "clone":
            document = template.model_copy(update={"templateId": target_id})
        if target_id != template.templateId:
            result.renames.append(
                LibraryRenameV1(
                    kind="template",
                    previousId=template.templateId,
                    importedId=target_id,
                )
            )
        store.register_template(document)
        result.templateIds.append(target_id)

    for index, experiment_id in experiment_skips:
        result.skipped.append(f"experiment:{index}:{experiment_id}")

    for template, index, template_hash, recipe, metric in experiment_plans:
        document = _new_experiment_document(
            template,
            bundle_id=bundle.bundleId,
            index=index,
            template_hash=template_hash,
            recipe=recipe,
            metric=metric,
            now=now,
        )
        store.save_experiment(document["experimentId"], document)
        result.experimentIds.append(document["experimentId"])

    return result
