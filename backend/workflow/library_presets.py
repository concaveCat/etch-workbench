"""Built-in library presets for a brand-new workbench.

A fresh install used to open the Library on ``Materials 0 / Processes 0 /
Flows 0 / Experiments 0``, which reads like a broken install rather than an
empty workspace. This module supplies two starter categories:

``material``
    One preset per entry of the verified ``GET /api/materials`` catalog. The
    preset references the catalog category and material name, and only adds
    presentation metadata (display name, catalog color, a role hint). It never
    copies density/conductivity, so the catalog stays authoritative and the
    write-path validator can still prove the engine material exists.

``process``
    Three step templates that a flow can insert directly: isotropic etch,
    isotropic deposit and single-particle etch. They are validated with the
    same ``TypeAdapter(StepV7)`` as the flow compiler, so a preset can never
    describe a step the engine would reject.

``flow``/``experiment`` are deliberately not seeded: a flow or experiment
preset would need a real definition/recipe to reference, and a dangling
reference is worse than an empty-state hint in the UI.

Seeding runs from ``WorkflowStore.initialize`` and is idempotent: it only fills
a category that is still empty, and preset ids are derived from the preset key
so a repeated initialize cannot duplicate rows or overwrite user entries.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .library_items import LibraryItemV1, validate_library_payload

# Catalog material names whose role hint is not the default "film".
SUBSTRATE_MATERIALS = frozenset({"Si"})
MASK_MATERIALS = frozenset({"Mask", "PMMA"})

DEFAULT_MATERIAL_ROLE = "film"

# (preset key, display name, description, StepV7 payload without ``id``).
PROCESS_PRESETS: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    (
        "isotropic-etch",
        "Isotropic Etch",
        "内置工艺预设：各向同性刻蚀，均匀速率。插入流程时会分配新的步骤 id。",
        {
            "operation": "etch",
            "model": "isotropic",
            "rateMagnitude": 0.1,
            "duration": 1.0,
            "rateMode": "uniform",
        },
    ),
    (
        "isotropic-deposit",
        "Isotropic Deposit",
        "内置工艺预设：各向同性沉积（SiO2），均匀速率。插入流程时会分配新的步骤 id。",
        {
            "operation": "deposit",
            "model": "isotropic",
            "rateMagnitude": 0.05,
            "duration": 1.0,
            "rateMode": "uniform",
            "depositMaterial": "SiO2",
        },
    ),
    (
        "single-particle-etch",
        "Single Particle Etch",
        "内置工艺预设：单粒子刻蚀，含粘附概率与源指数。插入流程时会分配新的步骤 id。",
        {
            "operation": "etch",
            "model": "singleParticle",
            "rateMagnitude": 0.1,
            "duration": 1.0,
            "rateMode": "uniform",
            "stickingProbability": 0.5,
            "sourceExponent": 1.0,
        },
    ),
)


def preset_item_id(category: str, key: str) -> str:
    """Return the stable item id of a built-in preset."""

    digest = hashlib.sha256(f"viennaps-workbench-preset:{category}:{key}".encode("utf-8")).hexdigest()
    return f"item_{digest[:24]}"


def material_role(name: str) -> str:
    """Return the presentation role hint for a catalog material."""

    if name in SUBSTRATE_MATERIALS:
        return "substrate"
    if name in MASK_MATERIALS:
        return "mask"
    return DEFAULT_MATERIAL_ROLE


def material_presets(now: str) -> list[LibraryItemV1]:
    """Build one preset per visible entry of the installed material catalog."""

    from ..materials import material_catalog

    catalog = material_catalog()
    rows = catalog.get("materials") if isinstance(catalog, dict) else None
    if not isinstance(rows, list):
        raise ValueError("installed native material catalog is unavailable")

    presets: list[LibraryItemV1] = []
    for row in rows:
        name = str(row["name"])
        category = str(row.get("category") or "Generic")
        payload = {
            "catalog": category,
            "material": name,
            "displayName": name,
            "color": str(row["color"]),
            "role": material_role(name),
        }
        presets.append(
            LibraryItemV1(
                itemId=preset_item_id("material", name),
                category="material",
                name=name,
                description=f"内置材料预设：{name}（{category}）。引用 GET /api/materials 目录。",
                payload=validate_library_payload(
                    "material", payload, item_name=name
                ),
                createdAt=now,
                updatedAt=now,
            )
        )
    return presets


def process_presets(now: str) -> list[LibraryItemV1]:
    """Build the built-in process step templates."""

    presets: list[LibraryItemV1] = []
    for key, name, description, payload in PROCESS_PRESETS:
        presets.append(
            LibraryItemV1(
                itemId=preset_item_id("process", key),
                category="process",
                name=name,
                description=description,
                payload=validate_library_payload(
                    "process", payload, item_name=name
                ),
                createdAt=now,
                updatedAt=now,
            )
        )
    return presets


def seed_library_defaults(store: Any, *, now: str) -> int:
    """Seed the built-in presets into a store; return the inserted row count."""

    documents = [preset.model_dump(mode="json") for preset in process_presets(now)]
    try:
        documents.extend(
            preset.model_dump(mode="json") for preset in material_presets(now)
        )
    except (OSError, ValueError, TypeError, AttributeError):
        # An unreadable catalog must not make the whole store unusable; the
        # process presets are still valid on their own.
        pass
    return store.seed_library_items(documents)
