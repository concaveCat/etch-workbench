"""Pure Python rate interpretation shared by API, hashing, supervision and worker."""
from __future__ import annotations

import math
from typing import Any

from process_models.v2.engines.viennaps import signed_rates as signed_rates

GEOMETRY_MODELS = {
    "removeMaterial", "planarize", "boolean", "gdsBoolean", "particleShape",
    "insertBox", "removeTopLevelSet",
}


def value(step: Any, key: str, default: Any = None) -> Any:
    return step.get(key, default) if isinstance(step, dict) else getattr(step, key, default)


def is_geometry_step(step: Any) -> bool:
    return value(step, "model") in GEOMETRY_MODELS


def has_motion(step: Any) -> bool:
    if value(step, "model") in {"specialized", "csvProcess", "implant", "anneal", "analysis"}:
        # Native specialized/CSV models own their physical rate law, and the
        # 4.7.0 cell-set field steps never use a schema-level scalar rate. A
        # schema-level scalar rate is intentionally absent; always execute.
        return True
    if value(step, "model") == "multiParticle":
        table = value(step, "rateCoefficients", {}) or {}
        defaults = value(table, "defaultBySpecies", {}) or {}
        overrides = value(table, "byMaterial", {}) or {}
        return any(float(rate) > 0.0 for rate in defaults.values()) or any(
            float(rate) > 0.0 for row in overrides.values() for rate in row.values()
        )
    if value(step, "model") == "directional":
        material_rates = value(step, "materialRates")
        default_material_rate = value(step, "defaultMaterialRate")
        if material_rates is not None or default_material_rate is not None:
            # Native RateMap mode: an unlisted material falls back to the
            # default row, so both the table and the default contribute motion.
            table = material_rates or {}
            if any(
                float(value(row, "directional", 0.0)) != 0.0
                or float(value(row, "isotropic", 0.0)) != 0.0
                for row in table.values()
            ):
                return True
            return (
                float(value(default_material_rate, "directional", 0.0)) != 0.0
                or float(value(default_material_rate, "isotropic", 0.0)) != 0.0
            )
        rate_sets = value(step, "rateSets")
        if rate_sets is not None:
            return any(
                float(value(item, "directionalVelocity", 0.0)) > 0.0
                or float(value(item, "isotropicVelocity", 0.0)) > 0.0
                for item in rate_sets
            )
        return (
            float(value(step, "directionalRateMagnitude", 0.0)) > 0.0
            or float(value(step, "isotropicRateMagnitude", 0.0)) > 0.0
        )
    fallback = float(value(step, "rateMagnitude", 0.0))
    mode = value(step, "rateMode", "uniform")
    if mode == "material":
        rates = value(step, "materialRates", {}) or {}
        return fallback > 0.0 or any(float(rate) > 0.0 for rate in rates.values())
    return fallback > 0.0


def uses_flux(step: Any) -> bool:
    if value(step, "model") == "specialized":
        from .specialized_models import model_spec
        spec = model_spec(str(value(step, "nativeModel", "")))
        return bool(spec and spec["usesFlux"])
    return has_motion(step) and value(step, "model") in {"singleParticle", "multiParticle"}


def signed_directional_rates(step: Any) -> tuple[float, float]:
    """Return signed native DirectionalProcess rates."""
    sign = -1.0 if value(step, "operation") == "etch" else 1.0
    return (
        float(value(step, "directionalRateMagnitude", 0.0)) * sign,
        float(value(step, "isotropicRateMagnitude", 0.0)) * sign,
    )


def signed_directional_rate_sets(step: Any) -> list[dict[str, Any]] | None:
    """Return one native-ready row per requested ``rateSets`` entry.

    ``None`` means the step uses the historical scalar fields. Each row keeps
    the requested values, the unit direction that actually reaches the native
    ``ps.RateSet`` and the signed native velocities. ``maskMaterials`` stays
    ``None`` when the request omitted it so the worker can fall back to the
    project's geometry-derived masks instead of the native ``[Mask]`` default.
    """
    rate_sets = value(step, "rateSets")
    if rate_sets is None:
        return None
    sign = -1.0 if value(step, "operation") == "etch" else 1.0
    rows: list[dict[str, Any]] = []
    for item in rate_sets:
        direction = [float(component) for component in value(item, "direction")]
        length = math.sqrt(sum(component * component for component in direction))
        requested_masks = value(item, "maskMaterials")
        directional = float(value(item, "directionalVelocity", 0.0))
        isotropic = float(value(item, "isotropicVelocity", 0.0))
        rows.append({
            "requestedDirection": direction,
            "nativeUnitDirection": [component / length for component in direction],
            "directionalRateMagnitude": directional,
            "isotropicRateMagnitude": isotropic,
            "nativeDirectionalVelocity": directional * sign,
            "nativeIsotropicVelocity": isotropic * sign,
            "requestedMaskMaterials": None if requested_masks is None else list(requested_masks),
            "requestedCalculateVisibility": bool(value(item, "calculateVisibility", True)),
        })
    return rows
