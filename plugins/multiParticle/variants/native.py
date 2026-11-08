"""Native multi-particle plasma variant.

The variant constructs the installed ``ps.MultiParticleProcess`` exactly like
``worker/simulate.py::build_multi_particle_model``: species are registered in
step order, then a fixed weighted-sum rate table is installed as the native
rate function. It never imports the worker, :mod:`backend`, or a native
library: ``ps`` always comes from the engine lease.

The rate function is deliberately data-driven and bounded, not user code:

``returned velocity = operationSign * sum(flux[i] * coefficient[material][species[i]])``

``operationSign`` is ``-1`` for etch and ``+1`` for deposit. Each override
falls back to the per-species default. The callback rejects non-finite fluxes,
a flux vector whose length differs from the configured species count, and a
material that is not registered in the native material registry. A bounded
audit (invocation count, per-material counts, up to 256 samples, flux sums and
maxima) is returned in the step metadata.

This slice declares CPU profiles only. The GPU multi-ion split strategy is
deferred and is not claimed here.

The variant mutates the caller's level-set handle in place
(``mutates_input=True``), exactly like the worker path. The executor is
responsible for checkpointing before calling it.
"""

from __future__ import annotations

import math
from typing import Any

from process_models.v2.contract import (
    EngineVariant,
    Issue,
    ProcessContext,
    StateProfile,
    VariantCapabilities,
)
from process_models.v2.engine import EngineLease, EngineRequirement, ExecutionProfile
from process_models.v2.engines import viennaps as _engine_helpers
from process_models.v2.engines.viennaps import (
    apply_execution_profile,
    configure_advection,
    configure_rays,
    material_handle,
    material_map,
)
from process_models.v2.state import (
    LevelSetState,
    MaterialState,
    ProcessState,
    StateDelta,
    StepResult,
)

from ..params import MultiParticleParams

__all__ = ["MultiParticleNativeVariant"]

_REQUIRED_PROJECT_KEYS = ("schemaVersion", "solver")
_SAMPLE_LIMIT = 256


class MultiParticleNativeVariant(EngineVariant):
    """Run one native ``ps.MultiParticleProcess`` step through shared helpers.

    Reads native geometry and materials, writes the mutated geometry plus the
    canonical material order. ``supports_masks=False``: neither the native
    model nor this variant consumes a geometry mask list. A deposit may
    introduce the deposited material, hence ``allow_new_materials=True``.
    """

    variant_id = "native"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry", "materials"}),
        writes=frozenset({"geometry", "materials"}),
        supports_masks=False,
        mutates_input=True,
        determinism="strict",
        state_profile=StateProfile(allow_new_materials=True),
    )
    engine = EngineRequirement(
        kind="native",
        engine_id="viennaps",
        min_version="4.6.2",
        profiles=(
            ExecutionProfile(backend="cpu", kernel="CPU_DISK"),
            ExecutionProfile(backend="cpu", kernel="CPU_TRIANGLE"),
        ),
    )
    implementation_hash: str | None = None
    implementation_files = (("process_models/v2/engines/viennaps.py", getattr(_engine_helpers, "__file__", "")),)

    def validate(self, ctx: ProcessContext, params: MultiParticleParams) -> list[Issue]:
        """Return semantic issues Pydantic and the resolver cannot express.

        Species labels, coefficient coverage, finite ranges, the operation
        sign, and the deposit material are all enforced by
        :class:`~plugins.multiParticle.params.MultiParticleParams`. Native
        material names are checked when the rate table or a neutral sticking
        map reaches the installed registry in :meth:`execute`.
        """
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: MultiParticleParams,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        """Apply one multi-particle step and return the committed state change.

        The native call sequence mirrors ``build_multi_particle_model``:
        species registration, fixed rate table, optional deposit level set,
        pinned flux engine, advection and ray parameters, then
        ``process.apply()``.
        """
        project = _require_project(ctx)
        geometry = state.require("geometry")
        ps = engine.handle
        domain = geometry.handle

        operation = str(params.operation)
        duration = float(params.duration)
        labels = params.labels()
        defaults = params.default_coefficients()
        overrides = params.material_coefficients()
        operation_sign = -1.0 if operation == "etch" else 1.0

        deposit_material: str | None = None
        if operation == "deposit":
            deposit_material = str(params.depositMaterial or "")
            if not deposit_material:
                raise RuntimeError("multiParticle deposit step requires depositMaterial")
            domain.duplicateTopLevelSet(material_handle(ps, deposit_material))

        model = ps.MultiParticleProcess()
        species_metadata: list[dict[str, Any]] = []
        for species in params.species:
            label = str(species.label)
            if species.kind == "ion":
                model.addIonParticle(
                    sourcePower=float(species.sourcePower),
                    thetaRMin=float(species.thetaRMin),
                    thetaRMax=float(species.thetaRMax),
                    minAngle=float(species.minAngle),
                    B_sp=float(species.B_sp),
                    meanEnergy=float(species.meanEnergy),
                    sigmaEnergy=float(species.sigmaEnergy),
                    thresholdEnergy=float(species.thresholdEnergy),
                    inflectAngle=float(species.inflectAngle),
                    n=int(species.n),
                    label=label,
                )
                species_metadata.append(species.model_dump())
            elif species.kind == "neutral":
                sticking_by_material = {
                    material_handle(ps, name): float(probability)
                    for name, probability in species.stickingProbabilityByMaterial.items()
                }
                model.addNeutralParticle(
                    sticking_by_material,
                    defaultStickingProbability=float(species.defaultStickingProbability),
                    label=label,
                )
                species_metadata.append(species.model_dump())
            else:  # pragma: no cover - the discriminated union already rejects this
                raise RuntimeError(f"unsupported multi-particle species kind {species.kind!r}")

        extra_material_names = set(state.materials.names.values())
        for species in params.species:
            if species.kind == "neutral":
                extra_material_names.update(species.stickingProbabilityByMaterial)
        native_material_names = _native_material_names(ps, extra_material_names)
        audit: dict[str, Any] = {
            "count": 0,
            "materials": {},
            "samples": [],
            "sampleLimit": _SAMPLE_LIMIT,
            "fluxSums": [0.0] * len(labels),
            "fluxMaxima": [0.0] * len(labels),
        }

        def rate_function(fluxes: Any, material: Any) -> float:
            material_repr = str(material)
            material_name = native_material_names.get(material_repr)
            if material_name is None:
                raise RuntimeError(f"ViennaPS rate callback returned an unregistered material {material_repr}")
            values = [float(value) for value in fluxes]
            if len(values) != len(labels):
                raise RuntimeError(
                    f"ViennaPS rate callback flux vector length {len(values)} does not match "
                    f"the {len(labels)} configured species"
                )
            if any(not math.isfinite(value) for value in values):
                raise RuntimeError("ViennaPS rate callback returned a non-finite species flux")
            for index, value in enumerate(values):
                audit["fluxSums"][index] += value
                audit["fluxMaxima"][index] = max(audit["fluxMaxima"][index], value)

            coefficients = overrides.get(material_name, {})
            magnitude = sum(
                values[index] * coefficients.get(label, defaults[label]) for index, label in enumerate(labels)
            )
            signed = operation_sign * magnitude
            if not math.isfinite(signed):
                raise RuntimeError("multi-particle rate relation returned a non-finite velocity")

            audit["count"] += 1
            audit["materials"][material_name] = audit["materials"].get(material_name, 0) + 1
            if len(audit["samples"]) < _SAMPLE_LIMIT:
                audit["samples"].append(
                    {
                        "material": material_name,
                        "fluxes": values,
                        "coefficients": {label: coefficients.get(label, defaults[label]) for label in labels},
                        "returnedVelocity": signed,
                    }
                )
            return signed

        model.setRateFunction(rate_function)

        process = ps.Process(domain, model, duration)
        execution = apply_execution_profile(ps, process, engine.profile)

        advection, advection_actual = configure_advection(ps, project)
        process.setParameters(advection)

        seed = ctx.seed
        if seed is None:
            random_settings = project.get("solver", {}).get("random") or {}
            if random_settings.get("mode") == "fixed" and random_settings.get("seed") is not None:
                seed = int(random_settings["seed"])
            else:
                raise RuntimeError(
                    "multiParticle requires ProcessContext.seed for a random-seed project; "
                    "the worker bridge passes the run seed"
                )
        rays, ray_actual = configure_rays(ps, project, int(seed))
        process.setParameters(rays)

        process.apply()

        material_order = tuple(int(material_id) for material_id in material_map(domain))
        delta_geometry = LevelSetState(
            handle=domain,
            dimension=geometry.dimension,
            grid_delta=geometry.grid_delta,
        )
        delta_materials = MaterialState(
            order=material_order,
            names={
                material_id: name
                for material_id, name in state.materials.names.items()
                if material_id in material_order
            },
        )

        metadata: dict[str, Any] = {
            "nativeClass": "ps.MultiParticleProcess",
            "nativeConstructor": "addIonParticle/addNeutralParticle + setRateFunction",
            "operation": operation,
            "duration": duration,
            "depositMaterial": deposit_material,
            "speciesOrder": list(labels),
            "species": species_metadata,
            "rateFunction": {
                "kind": "weighted-sum-table",
                "formula": "operationSign * sum(flux[i] * coefficient[material][species[i]])",
                "operationSign": operation_sign,
                "defaultBySpecies": defaults,
                "byMaterial": overrides,
                "nativeFluxUnits": "not-declared-by-installed-binding",
                "returnedVelocityUnit": "um/s workbench convention; coefficients perform the conversion",
            },
            "rateCallback": audit,
            "nativeSpeciesFieldNames": [f"{label}{index}" for index, label in enumerate(labels)],
            "timeStepRatio": float(advection.timeStepRatio),
            "timeStepRatioSource": advection_actual.get("timeStepRatioSource", "solver.timeStepRatio"),
            "rayTracing": ray_actual,
            "fluxEngine": execution["kernel"],
            "executionProfile": execution,
            "dimension": geometry.dimension,
            "gridDelta": geometry.grid_delta,
            "levelSetCount": int(domain.getNumberOfLevelSets()),
            "materialMap": list(material_order),
        }

        return StepResult(
            delta=StateDelta(geometry=delta_geometry, materials=delta_materials),
            metadata=metadata,
            warnings=[],
        )


def _native_material_names(ps: Any, extra_names: Any = ()) -> dict[str, str]:
    """Map the native callback's material representation to its registered name.

    The installed callback receives a ``Material`` handle and its string form
    (``"Material('Si')"``) is accepted by the worker's reference path. Deriving
    the map from ``ps.Material`` keeps this plugin independent of
    :mod:`backend` and :mod:`worker`; names carried by the state or a neutral
    sticking map are added when the installed registry knows them.
    """
    registry = ps.MaterialRegistry.instance()
    names: dict[str, str] = {}
    for name in dir(ps.Material):
        if name.startswith("_"):
            continue
        if registry.findMaterial(name) is None:
            continue
        try:
            handle = registry.getMaterial(name)
        except (TypeError, ValueError):  # pragma: no cover - defensive native access
            continue
        names[str(handle)] = name
    for name in extra_names:
        if not isinstance(name, str) or not name.strip():
            continue
        if registry.findMaterial(name) is None:
            continue
        names[str(registry.getMaterial(name))] = name
    return names


def _require_project(ctx: ProcessContext) -> Any:
    """Return ``ctx.project`` or raise when a required key is missing."""
    project = ctx.project
    missing = [name for name in _REQUIRED_PROJECT_KEYS if not project.get(name)]
    if missing:
        raise RuntimeError(
            "multiParticle native variant requires a project with "
            + ", ".join(_REQUIRED_PROJECT_KEYS)
            + f"; missing {', '.join(missing)}"
        )
    return project
