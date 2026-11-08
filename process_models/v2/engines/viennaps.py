"""ViennaPS helpers shared by the worker and v2 plugin variants.

These functions were extracted from :mod:`worker.simulate` so a plugin variant
can drive a native ``ps`` module without importing the worker. They are pure
Python glue: the native module is always passed in as ``ps`` and nothing here
imports ViennaPS, ViennaLS, :mod:`backend`, or :mod:`worker`.

:func:`signed_rates` and :func:`geometry_mask_material_names` are defined here
because a plugin needs them; :mod:`backend.rates` and :mod:`backend.materials`
re-export them for existing API/worker callers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Iterable

from ..materials import MaterialRegistryError

__all__ = [
    "GEO_MASK_MATERIAL_DEFAULT",
    "ViennaPSMaterialRegistry",
    "apply_execution_profile",
    "configure_advection",
    "geometry_mask_material_names",
    "material_handle",
    "material_map",
    "protected_material_names",
    "signed_rates",
]

#: Material assumed for the built-in trench/hole/fin mask geometry.
GEO_MASK_MATERIAL_DEFAULT = "Mask"


def _step_value(step: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a step mapping or object."""
    return step.get(key, default) if isinstance(step, dict) else getattr(step, key, default)


def signed_rates(step: Any) -> tuple[float, dict[str, float]]:
    """Return native default and explicit material rates, signed for etch."""
    sign = -1.0 if _step_value(step, "operation") == "etch" else 1.0
    fallback = float(_step_value(step, "rateMagnitude", 0.0)) * sign
    rates = {name: float(rate) * sign for name, rate in (_step_value(step, "materialRates", {}) or {}).items()}
    return fallback, rates


def geometry_mask_material_names(geometry: Any) -> list[str]:
    """Material names exposed as masks by the project geometry alone.

    This is the single derivation shared by the scalar particle/directional
    paths, the CSV rate-grid path and the per-rate-set mask default. It covers
    the built-in trench/hole/fin mask, stack mask layers and the legacy stack
    ``gdsMask`` overlay (only when it protects etch), plus GDS geometry layers
    explicitly marked as masks.
    """
    if not isinstance(geometry, dict):
        return []
    kind = geometry.get("kind")
    if kind in ("trench", "hole", "fin") and float(geometry.get("maskHeight", 0) or 0) > 0:
        return [str(geometry.get("maskMaterial", GEO_MASK_MATERIAL_DEFAULT))]
    if kind == "stack":
        values = [str(layer["material"]) for layer in geometry.get("layers", []) if layer.get("role") == "mask"]
        gds_mask = geometry.get("gdsMask") or {}
        if gds_mask.get("protectsEtch") and gds_mask.get("material"):
            values.append(str(gds_mask["material"]))
        return list(dict.fromkeys(values))
    if kind == "gds":
        return list(
            dict.fromkeys(str(layer["material"]) for layer in geometry.get("layers", []) if layer.get("mask", False))
        )
    return []


def configure_rays(ps: Any, project: dict[str, Any], seed: int) -> tuple[Any, dict[str, Any]]:
    """Build deterministic ray-tracing parameters for a flux step."""
    params = ps.RayTracingParameters()
    params.raysPerPoint = int(project["solver"]["raysPerPoint"])
    params.rngSeed = int(seed)
    params.useRandomSeeds = False
    actual: dict[str, Any] = {
        "raysPerPoint": int(params.raysPerPoint),
        "rngSeed": int(params.rngSeed),
        "useRandomSeeds": bool(params.useRandomSeeds),
    }
    if project["schemaVersion"] >= 7:
        requested = project["solver"]["rayTracing"]
        for name in (
            "diskRadius",
            "ignoreFluxBoundaries",
            "maxBoundaryHits",
            "maxReflections",
            "minNodeDistanceFactor",
            "smoothingNeighbors",
        ):
            setattr(params, name, requested[name])
        params.normalizationType = getattr(ps.NormalizationType, requested["normalizationType"])
        actual.update(
            {
                name: getattr(params, name)
                for name in (
                    "diskRadius",
                    "ignoreFluxBoundaries",
                    "maxBoundaryHits",
                    "maxReflections",
                    "minNodeDistanceFactor",
                    "smoothingNeighbors",
                )
            }
        )
        actual["normalizationType"] = params.normalizationType.name
        actual["normalizationTypeValue"] = int(params.normalizationType)
        actual["nativeRayTracingMetadata"] = params.toMetaData()
    return params, actual


def apply_execution_profile(ps: Any, process: Any, profile: Any) -> dict[str, Any]:
    """Apply a resolved execution profile to a native ``ps.Process``.

    The plugin passes the profile it received from the engine lease; it never
    inspects ``solver.enginePreference`` and never writes a literal kernel. A
    profile without a kernel leaves the native default untouched (useful for
    non-flux models and custom engines). An unknown kernel is a hard error.
    """
    kernel = getattr(profile, "kernel", None)
    if kernel is not None:
        engine_type = getattr(ps.FluxEngineType, str(kernel), None)
        if engine_type is None:
            raise RuntimeError(f"installed engine has no FluxEngineType {kernel!r}")
        process.setFluxEngineType(engine_type)
    return {
        "backend": getattr(profile, "backend", None),
        "kernel": kernel,
        "device": getattr(profile, "device", None),
        "strategy": getattr(profile, "strategy", None),
    }


@dataclass(frozen=True)
class ViennaPSMaterialRegistry:
    """Material registry backed by the installed ViennaPS module."""

    ps: Any

    def id_for(self, name: str) -> int:
        material = getattr(self.ps.Material, str(name), None)
        if material is None:
            native = self.ps.MaterialRegistry.instance().findMaterial(str(name))
            material = native
        if material is None:
            raise MaterialRegistryError(f"material {name!r} is absent from the installed ViennaPS MaterialRegistry")
        try:
            return int(material)
        except (TypeError, ValueError) as error:
            raise MaterialRegistryError(f"material {name!r} has no integer native id") from error

    def name_for(self, material_id: int) -> str | None:
        target = int(material_id)
        for name in dir(self.ps.Material):
            if name.startswith("_"):
                continue
            try:
                if int(getattr(self.ps.Material, name)) == target:
                    return name
            except (TypeError, ValueError):
                continue
        return None

    def require(self, names: Iterable[str]) -> tuple[int, ...]:
        return tuple(self.id_for(name) for name in names)


def material_handle(ps: Any, name: str) -> Any:
    """Return the installed ViennaPS material handle for ``name``.

    Raises ``RuntimeError`` instead of silently substituting a default when the
    name is absent from the native ``MaterialRegistry``.
    """
    registry = ps.MaterialRegistry.instance()
    material = registry.findMaterial(name)
    if material is None:
        raise RuntimeError(f"material {name!r} is absent from the native MaterialRegistry")
    return registry.getMaterial(name)


def material_map(domain: Any) -> list[int]:
    """Return the material id of every level set in ``domain``, in order."""
    mapping = domain.getMaterialMap()
    return [int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size())]


def protected_material_names(project: dict[str, Any], step: dict[str, Any] | None = None) -> list[str]:
    """Material names that a native process must not move.

    The geometry derivation lives next to this helper (and is re-exported by
    ``backend.materials``) so the API validation, the worker and the plugins
    share one implementation. This helper keeps the ``rateMode == "material"``
    rule: the rate table is authoritative there, so geometry masks are not
    applied.
    """
    if project["schemaVersion"] in (4, 5, 6, 7) and step is not None and step.get("rateMode") == "material":
        return []
    return geometry_mask_material_names(project["geometry"])


def configure_advection(
    ps: Any,
    project: dict[str, Any],
    time_step_ratio: float | None = None,
    override: Any | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Build the step's native AdvectionParameters.

    ``time_step_ratio`` is the step-level ``isotropicTimeStepRatio`` override.
    ``override`` is an optional :class:`process_models.v2.options.AdvectionOverride`
    (or a mapping with the same fields). It is applied after the project
    defaults to a freshly configured parameter object, never to the saved
    project's solver block, and every applied value is recorded so a run stays
    auditable.

    ``maxDissipation`` is the native static
    ``StencilLocalLaxFriedrichsScalar.setMaxDissipation`` value. It is only
    meaningful for the stencil-local first-order scheme, so this helper rejects
    it when neither the step nor the project selects that scheme.
    """
    params = ps.AdvectionParameters()
    params.timeStepRatio = float(project["solver"]["timeStepRatio"])
    actual: dict[str, Any] = {"timeStepRatio": float(params.timeStepRatio)}
    if time_step_ratio is not None:
        params.timeStepRatio = float(time_step_ratio)
        actual["timeStepRatio"] = float(params.timeStepRatio)
        actual["solverTimeStepRatio"] = float(project["solver"]["timeStepRatio"])
        actual["timeStepRatioSource"] = "step.isotropicTimeStepRatio"
    project_advection = None
    if project["schemaVersion"] >= 7:
        project_advection = project["solver"]["advection"]
        params.spatialScheme = getattr(ps.ls.SpatialSchemeEnum, project_advection["spatialScheme"])
        params.temporalScheme = getattr(ps.ls.TemporalSchemeEnum, project_advection["temporalScheme"])
        for name in (
            "adaptiveTimeStepping",
            "adaptiveTimeStepSubdivisions",
            "calculateIntermediateVelocities",
            "checkDissipation",
            "dissipationAlpha",
            "ignoreVoids",
            "velocityOutput",
        ):
            setattr(params, name, project_advection[name])
        actual.update(
            {
                name: getattr(params, name)
                for name in (
                    "adaptiveTimeStepping",
                    "adaptiveTimeStepSubdivisions",
                    "calculateIntermediateVelocities",
                    "checkDissipation",
                    "dissipationAlpha",
                    "ignoreVoids",
                    "velocityOutput",
                )
            }
        )
        actual["spatialScheme"] = params.spatialScheme.name
        actual["spatialSchemeValue"] = int(params.spatialScheme)
        actual["temporalScheme"] = params.temporalScheme.name
        actual["temporalSchemeValue"] = int(params.temporalScheme)
        actual["nativeAdvectionMetadata"] = params.toMetaData()
    if override is not None:

        def field(name: str) -> Any:
            if isinstance(override, Mapping):
                return override.get(name)
            return getattr(override, name, None)

        requested_spatial = field("spatialScheme")
        requested_temporal = field("temporalScheme")
        requested_ratio = field("timeStepRatio")
        max_dissipation = field("maxDissipation")
        if requested_spatial is not None:
            params.spatialScheme = getattr(ps.ls.SpatialSchemeEnum, str(requested_spatial))
            actual["spatialScheme"] = params.spatialScheme.name
            actual["spatialSchemeValue"] = int(params.spatialScheme)
            actual["spatialSchemeSource"] = "step.advection"
        if requested_temporal is not None:
            params.temporalScheme = getattr(ps.ls.TemporalSchemeEnum, str(requested_temporal))
            actual["temporalScheme"] = params.temporalScheme.name
            actual["temporalSchemeValue"] = int(params.temporalScheme)
            actual["temporalSchemeSource"] = "step.advection"
        if requested_ratio is not None:
            params.timeStepRatio = float(requested_ratio)
            actual["timeStepRatio"] = float(params.timeStepRatio)
            actual["solverTimeStepRatio"] = float(project["solver"]["timeStepRatio"])
            actual["timeStepRatioSource"] = "step.advection.timeStepRatio"
        actual["overrideApplied"] = True
        actual["overrideFields"] = sorted(
            name
            for name in ("spatialScheme", "temporalScheme", "timeStepRatio", "maxDissipation")
            if field(name) is not None
        )
        if max_dissipation is not None:
            effective_scheme = (
                str(requested_spatial)
                if requested_spatial is not None
                else str(project_advection["spatialScheme"])
                if project_advection is not None
                else ""
            )
            if effective_scheme != "STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER":
                raise ValueError(
                    "advection.maxDissipation requires spatialScheme="
                    "STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER (step or project); "
                    f"effective scheme is {effective_scheme or 'native-default'!r}"
                )
            ps.StencilLocalLaxFriedrichsScalar.setMaxDissipation(float(max_dissipation))
            actual["maxDissipation"] = float(max_dissipation)
            actual["maxDissipationSource"] = "step.advection.staticStencilLocalLaxFriedrichs"
        actual["nativeAdvectionMetadata"] = params.toMetaData()
    return params, actual
