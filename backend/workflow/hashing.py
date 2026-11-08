"""Content hashes shared by the compiler and the persisted compile envelope."""

from __future__ import annotations

from typing import Any, Iterable

from .ids import sha256_hex


def execution_project_payload(project: Any) -> dict[str, Any]:
    """Project payload used for the execution compile hash.

    Name and view settings are presentation-only. They must not make two
    otherwise identical execution specifications hash differently.
    """

    payload = project.model_dump(mode="json", exclude_none=True)
    for key in ("name", "view", "view3D"):
        payload.pop(key, None)
    return payload


def full_project_hash(project: Any) -> str:
    return sha256_hex(execution_project_payload(project))


def source_map_hash(entries: Iterable[Any]) -> str:
    rows = [
        entry.model_dump(mode="json", exclude_none=True)
        if hasattr(entry, "model_dump")
        else entry
        for entry in entries
    ]
    return sha256_hex(rows)


def compile_record_id(
    *,
    compiler_version: str,
    flow_revision_hash: str,
    geometry_binding_hash: str,
    material_definitions_hash: str,
    variable_set: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
    seed_policy: str,
    step_id_policy: str,
    engine_versions: dict[str, str],
    module_revisions: list[dict[str, Any]] | None = None,
) -> str:
    identity = {
        "compilerVersion": compiler_version,
        "flowRevisionHash": flow_revision_hash,
        "geometryBindingHash": geometry_binding_hash,
        "materialDefinitionsHash": material_definitions_hash,
        "variableSet": variable_set,
        "overrides": overrides,
        "seedPolicy": seed_policy,
        "stepIdPolicy": step_id_policy,
        "engineVersions": engine_versions,
    }
    # Keep the P0 identity byte-for-byte stable when no modules are involved.
    if module_revisions:
        normalized = []
        for value in module_revisions:
            payload = value.model_dump(mode="json", exclude_none=True) if hasattr(value, "model_dump") else dict(value)
            normalized.append({"moduleId": str(payload["moduleId"]), "revision": int(payload["revision"])})
        identity["moduleRevisions"] = sorted(
            normalized, key=lambda entry: (entry["moduleId"], entry["revision"])
        )
    return f"c_{sha256_hex(identity)[:24]}"
