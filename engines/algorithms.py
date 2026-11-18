"""Master algorithm registry loader, live introspector, and active-default switcher.

Connects the declarative selection table in ``engines/algorithms.json`` with the
live :class:`~engines.sdk.models.StepModelRegistry` of each engine so users and
agents can:

1. Inspect every concrete algorithm, its theoretical/contract origin, fidelity, and
   source file across ``voxel``, ``viennaps``, and ``spx``;
2. Switch the default algorithm for any ``<kind>/<mechanism>`` slot globally via
   ``engines/algorithms.json`` (or ``python -m engines.cli set-algorithm``), per-run
   via ``JobSpec.algorithms`` / ``--algo``, or per-step via ``StepIR.model``;
3. Register new algorithms in code or external ``VIENNAPS_STEP_MODEL_MODULES`` plugins
   with full structured provenance metadata.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from engines.protocol import write_json

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "algorithms.json"


def config_path() -> Path:
    override = os.environ.get("VIENNAPS_ALGORITHMS_CONFIG", "").strip()
    return Path(override) if override else DEFAULT_CONFIG_PATH


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {
            "schema_version": 1,
            "algorithm_types": {},
            "active_defaults": {},
            "primitives": [],
            "reference_engines": {},
        }
    return json.loads(path.read_text(encoding="utf-8"))


def active_default(engine_id: str, kind: str, mechanism: str) -> str | None:
    cfg = load_config()
    per_engine = (cfg.get("active_defaults") or {}).get(engine_id) or {}
    slot = f"{kind}/{mechanism}"
    if slot in per_engine:
        return str(per_engine[slot])
    if f"*/{mechanism}" in per_engine:
        return str(per_engine[f"*/{mechanism}"])
    return None


def live_sdk_registries() -> dict[str, Any]:
    """Build live StepModelRegistry instances for built-in SDK engines."""
    registries: dict[str, Any] = {}
    try:
        from engines.voxel.models import build_default_registry

        reg = build_default_registry()
        reg.load_external_plugins()
        registries["voxel"] = reg
    except Exception:  # noqa: BLE001
        pass

    try:
        from engines.viennaps.engine import build_viennaps_models

        reg_vp = build_viennaps_models()
        reg_vp.load_external_plugins()
        registries["viennaps"] = reg_vp
    except Exception:  # noqa: BLE001
        pass

    return registries


def set_active_default(engine_id: str, slot: str, model_id: str) -> dict[str, Any]:
    """Validate ``model_id`` on ``engine_id`` and persist as default for ``slot``."""
    if "/" not in slot:
        raise ValueError(
            f"slot must be '<kind>/<mechanism>' (e.g. 'mask/litho' or 'etch/ellipsoidal'), got {slot!r}"
        )
    registries = live_sdk_registries()
    if engine_id in registries:
        reg = registries[engine_id]
        if not reg.has_model(model_id):
            raise ValueError(
                f"engine {engine_id!r} has no registered algorithm {model_id!r}; "
                f"available: {reg.registered_models()}"
            )
    path = config_path()
    cfg = load_config()
    defaults = cfg.setdefault("active_defaults", {})
    engine_map = defaults.setdefault(engine_id, {})
    previous = engine_map.get(slot)
    engine_map[slot] = model_id
    write_json(path, cfg)
    return {
        "configPath": str(path),
        "engineId": engine_id,
        "slot": slot,
        "previousModel": previous,
        "activeModel": model_id,
    }


def catalog(engine_filter: str | None = None) -> dict[str, Any]:
    """Return the complete unified algorithm catalog across all engines."""
    cfg = load_config()
    sdk_regs = live_sdk_registries()
    engines_out: dict[str, Any] = {}

    for eid, reg in sdk_regs.items():
        if engine_filter and eid != engine_filter:
            continue
        engines_out[eid] = {
            "engineId": eid,
            "configurable": True,
            "activeDefaults": dict((cfg.get("active_defaults") or {}).get(eid) or {}),
            "algorithms": reg.describe_algorithms(),
        }

    for ref_id, rows in (cfg.get("reference_engines") or {}).items():
        if engine_filter and ref_id != engine_filter:
            continue
        engines_out[ref_id] = {
            "engineId": ref_id,
            "configurable": False,
            "activeDefaults": {row["slot"]: row["model_id"] for row in rows},
            "algorithms": list(rows),
        }

    primitives = [
        p for p in (cfg.get("primitives") or [])
        if not engine_filter or p.get("engine_id") == engine_filter
    ]
    return {
        "schemaVersion": cfg.get("schema_version", 1),
        "configPath": str(config_path()),
        "algorithmTypes": cfg.get("algorithm_types") or {},
        "engines": engines_out,
        "primitives": primitives,
    }


def format_table(cat: dict[str, Any]) -> str:
    """Render a concise human-readable table for CLI output."""
    lines: list[str] = []
    lines.append(f"Config file: {cat['configPath']}")
    for eid, info in cat["engines"].items():
        mode = "configurable" if info["configurable"] else "reference"
        lines.append(f"\n=== Engine: {eid} ({mode}) ===")
        lines.append(
            f"  {'SLOT':<21} {'ACTIVE':<6} {'MODEL_ID':<32} {'TYPE':<20} {'FIDELITY':<12} ORIGIN"
        )
        for row in info["algorithms"]:
            slot = row.get("slot") or "custom/*"
            mid = row["model_id"]
            is_active = info["activeDefaults"].get(slot) == mid or bool(row.get("is_active"))
            mark = "  *   " if is_active else "      "
            atype = row.get("algorithm_type", "custom")
            fid = row.get("fidelity", "custom")
            origin = row.get("origin", "")
            lines.append(
                f"  {slot:<21} {mark:<6} {mid:<32} {atype:<20} {fid:<12} {origin}"
            )
    if cat.get("primitives"):
        lines.append("\n=== Shared Geometric & Probing Primitives ===")
        for p in cat["primitives"]:
            lines.append(
                f"  [{p['engine_id']:<8}] {p['primitive_id']:<36} ({p['algorithm_type']}) -> {p['origin']}"
            )
    return "\n".join(lines)
