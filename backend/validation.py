"""Application resource checks layered on top of the frozen JSON schema."""

from __future__ import annotations

import math
import json
from importlib.metadata import PackageNotFoundError, version
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import Domain3D, Project, ProjectV3, ProjectV4, ProjectV5, ProjectV6, ProjectV7, parse_project
from .materials import attach_and_reconcile_project_materials, geometry_mask_material_names
from .rates import has_motion, is_geometry_step, uses_flux, value as step_value
from .specialized_models import (FLUOROCARBON_MATERIAL_DEFAULTS, IBE_ENERGY_MODELS,
                                 faraday_cage_2d_engine_error, geometric_trench_issues,
                                 ibe_energy_issues, nv_factors_issues, model_spec,
                                 oxidation_advanced_spec)
from .runtime_limits import available_cpu_threads
from .engine import (EVIDENCE_ENGINE_VERSION, OXIDATION_ADVANCED_RESTRICTED,
                     ald_surface_diffusion_error, ald_surface_diffusion_supported,
                     engine_version, multi_ion_gpu_error, oxidation_advanced_supported)


# These ViennaPS 4.6.2 combinations either SIGSEGV or produce different final
# meshes from the independent product sequence when paired with native
# d2/d3.MakeFin. Keep old project data intact, but resolve those runs through
# the independently verified Boolean construction.
NATIVE_MAKEFIN_SIGSEGV_MODELS = frozenset({"sf6o2", "hbro2", "sf6c4f8", "neutralTransport"})
GPU_MULTIPARTICLE_SPLIT_STRATEGY = "symmetric-operator-splitting-v1"
GPU_MULTIPARTICLE_FLUX_STRATEGY = "per-species-native-mesh-merge-v1"
GPU_MULTIPARTICLE_NATIVE_STRATEGY = "native-multi-ion-v1"
GPU_MULTIPARTICLE_NATIVE_FLUX_STRATEGY = "native-multi-ion-flux-v1"


def fin_construction_decision(project: dict[str, Any]) -> dict[str, Any]:
    geometry = project.get("geometry", {})
    requested = str(geometry.get("construction", "native")) if geometry.get("kind") == "fin" else "native"
    enabled_models = {
        str(step.get("nativeModel"))
        for step in project.get("steps", [])
        if step.get("enabled") and step.get("model") == "specialized"
        and step.get("nativeModel") in NATIVE_MAKEFIN_SIGSEGV_MODELS
    }
    makefin_fallback_models = sorted(enabled_models) if requested == "native" else []
    domain = project.get("domain", {})
    boundary = domain.get("boundaryXY", domain.get("xBoundary", "reflective"))
    periodic_3d_fin = (project.get("dimension") == 3 and geometry.get("kind") == "fin"
                       and boundary == "periodic")
    generic_masked_models = sorted({
        str(step.get("model")) for step in project.get("steps", [])
        if step.get("enabled") and step.get("model") in {"singleParticle", "multiParticle"}
    }) if (project.get("dimension") == 3 and geometry.get("kind") == "fin"
           and float(geometry.get("maskHeight", 0)) > 0) else []
    periodic_masked_generic_models = generic_masked_models if periodic_3d_fin else []
    generic_construction_fallback_models = (generic_masked_models if requested == "native" else [])
    extrusion_fallback_models = sorted(enabled_models | set(periodic_masked_generic_models)) if (
        periodic_3d_fin and geometry.get("extrusion") is not None
    ) else []
    periodic_masked_fallback = bool(generic_construction_fallback_models and periodic_3d_fin)
    effective = "boolean" if makefin_fallback_models else requested
    if generic_construction_fallback_models:
        effective = "boolean"
    extrusion_route = ("direct-stencil" if extrusion_fallback_models else "d2-extrude") \
        if geometry.get("extrusion") is not None else "direct"
    return {"requested": requested, "effective": effective,
            "fallbackModels": makefin_fallback_models,
            "constructionFallbackModels": sorted(set(makefin_fallback_models) | set(generic_construction_fallback_models)),
            "constructionFallbackReason": ("native-makefin-sigsegv" if makefin_fallback_models else
                                            "periodic-masked-fin-geometry" if periodic_masked_fallback else
                                            "masked-3d-fin-generic-process" if generic_construction_fallback_models else None),
            "extrusionRoute": extrusion_route,
            "extrusionFallbackModels": extrusion_fallback_models}


@dataclass(frozen=True)
class FieldIssue:
    path: str
    message: str


class ProjectInputError(ValueError):
    def __init__(self, issues: list[FieldIssue]):
        self.issues = issues
        super().__init__("; ".join(f"{item.path}: {item.message}" for item in issues))


def gpu_disk_verified() -> bool:
    capability_path = Path(__file__).resolve().parents[1] / "docs" / "capabilities.json"
    try:
        capabilities = json.loads(capability_path.read_text(encoding="utf-8"))
        gpu = capabilities.get("engines", {}).get("gpu", {})
        return gpu.get("status") == "verified-host" and gpu.get("exposed") is True and gpu.get("engine") == "GPU_DISK"
    except (OSError, ValueError, AttributeError):
        return False


def _path(parts: tuple[Any, ...]) -> str:
    text = ""
    for part in parts:
        if isinstance(part, int):
            text += f"[{part}]"
        elif text:
            text += f".{part}"
        else:
            text = str(part)
    return text or "$"


def _parse(payload: Any) -> Project:
    if isinstance(payload,dict) and (type(payload.get("schemaVersion")) is not int or payload.get("schemaVersion") not in (1,2,3,4,5,6,7)):
        raise ProjectInputError([FieldIssue("schemaVersion","supported schema versions are 1 (2D), 2 (3D), 3, 4, 5, 6 and 7")])
    try:
        return parse_project(payload)
    except ValidationError as exc:
        issues = []
        for error in exc.errors():
            path = _path(tuple(error["loc"]))
            if path == "$" and "step IDs" in str(error["msg"]):
                path = "steps"
            issues.append(FieldIssue(path, str(error["msg"])))
        raise ProjectInputError(issues) from exc


def known_native_failure(dimension: int, model: str, operation: str, boundary: str,
                        geometry_kind: str | None, engine: str | None = None) -> dict[str, Any] | None:
    """Return an exact installed-version native crash record, if one is measured."""
    if geometry_kind != "gds":
        return None
    try:
        installed_version = version("viennaps")
        matrix_path = Path(__file__).resolve().parents[1] / "docs" / "stage9-evidence" / "gpu-capability-matrix.json"
        failures = json.loads(matrix_path.read_text()).get("knownNativeFailures", [])
    except (OSError, ValueError, PackageNotFoundError, AttributeError, TypeError):
        return None
    for failure in failures:
        if not all(failure.get(key) == value for key, value in {
            "dimension": dimension, "model": model, "operation": operation,
            "boundaryXY": boundary, "geometryKind": geometry_kind,
            "engineVersion": installed_version,
        }.items()):
            continue
        engines = failure.get("engines", [])
        if engine is None or engine in engines:
            return failure
    return None


def known_engine_limitation(dimension: int, model: str, engine: str,
                            step: dict[str, Any]) -> dict[str, Any] | None:
    """Return a measured installed-engine species limit for this step."""
    if model != "multiParticle" or engine != "GPU_DISK":
        return None
    ion_count = sum(1 for species in step.get("species", []) if species.get("kind") == "ion")
    if ion_count <= 1:
        return None
    try:
        installed_version = version("viennaps")
        matrix_path = Path(__file__).resolve().parents[1] / "docs" / "stage9-evidence" / "gpu-capability-matrix.json"
        limitations = json.loads(matrix_path.read_text()).get("knownEngineLimitations", [])
    except (OSError, ValueError, PackageNotFoundError, AttributeError, TypeError):
        return None
    return next((row for row in limitations if row.get("model") == model
                 and row.get("engine") == engine and row.get("engineVersion") == installed_version
                 and dimension in row.get("dimensions", []) and ion_count > row.get("maxIonSpecies", 0)), None)


def validate_project(payload: Any, *, kind: str = "project") -> tuple[Project, list[str]]:
    project = _parse(payload)
    issues: list[FieldIssue] = []
    warnings: list[str] = []

    if project.schemaVersion in (3, 4, 5, 6, 7):
        try:
            attach_and_reconcile_project_materials(project)
        except (ValueError, OSError) as exc:
            issues.append(FieldIssue("materialDefinitions", str(exc)))

    if kind == "simulate" and not any(step.get("enabled") if isinstance(step, dict) else step.enabled for step in project.steps):
        issues.append(FieldIssue("steps", "simulate requires at least one enabled process step"))

    delta = project.domain.gridDelta
    extent = project.domain.xExtent
    if project.schemaVersion in (3,4,5,6,7):
        issues.extend(_validate_materials(
            project, enforce_gds_domain=kind in ("simulate", "preview", "flux")))
        warnings.extend(_planarize_warnings(project))
        warnings.extend(_gds_boolean_warnings(project))
        warnings.extend(_directional_rate_set_warnings(project))
        issues.extend(_validate_isotropic_dispatch(project))
        warnings.extend(_isotropic_dispatch_warnings(project))
        if project.schemaVersion == 7:
            warnings.extend(_specialized_model_warnings(project))
        if project.schemaVersion == 7:
            issues.extend(_validate_specialized_steps(project))
            csv_issues, csv_warnings = _validate_csv_process_steps(project)
            issues.extend(csv_issues)
            warnings.extend(csv_warnings)
            issues.extend(_validate_particle_shape_steps(project))
            warnings.extend(_particle_shape_warnings(project))
            issues.extend(_validate_implant_steps(project))
        if project.dimension == 3:
            issues.extend(_validate_v3_3d_geometry(project))
        else:
            issues.extend(_validate_v3_2d_geometry(project))
        if project.solver.cpuThreads > available_cpu_threads():
            issues.append(FieldIssue("solver.cpuThreads", "thread count exceeds available CPU threads"))
        normalized=project.model_dump(mode="json",exclude_none=True)
        if project.schemaVersion == 7 and project.solver.engineId is not None:
            try:
                from process_models.engine_plugins import EngineContext, load_engine_registry
                engine_registry = load_engine_registry()
                installed = {plugin.engine_id for plugin in engine_registry.plugins}
                if project.solver.engineId not in installed:
                    issues.append(FieldIssue("solver.engineId", f"engine {project.solver.engineId!r} is not installed; add its engine.toml and run scripts/lock-engines.py"))
                else:
                    provider = engine_registry.provider(
                        EngineContext(project=normalized, run_dir=Path.cwd(), fingerprint="api-validation")
                    )
                    kernel = project.solver.engineKernel
                    compatible = [
                        capability for capability in provider.engines()
                        if capability.engine_id == project.solver.engineId
                        and (not capability.kernels or kernel in capability.kernels)
                    ]
                    if not compatible:
                        issues.append(FieldIssue("solver.engineKernel", f"engine {project.solver.engineId!r} does not advertise kernel {kernel!r}"))
            except Exception as error:  # noqa: BLE001 - validation must report, not crash
                issues.append(FieldIssue("solver.engineId", f"engine plugin validation failed: {error}"))
        boundary=normalized["domain"].get("boundaryXY",normalized["domain"].get("xBoundary"))
        for i,step in enumerate(project.steps):
            step_dict=step.model_dump(mode="json",exclude_none=True) if not isinstance(step,dict) else step
            if project.schemaVersion == 7 and step_value(step, "model") == "specialized":
                # Specialized models have their own per-model CPU/GPU matrix;
                # do not skip this exact gate with the generic particle path.
                model_key = f"specialized:{step_value(step, 'nativeModel')}"
                installed_model = model_spec(step_value(step, "nativeModel"))
                native_engine = select_engine(normalized, step_dict) if installed_model and installed_model["usesFlux"] else "not-applicable"
                native_failure = known_native_failure(
                    project.dimension, model_key, step_value(step, "operation"), boundary,
                    normalized.get("geometry", {}).get("kind"), native_engine)
                if step_value(step, "enabled") and native_failure and not native_engine.startswith("GPU_"):
                    message = (f"installed ViennaPS {native_failure['engineVersion']} reproduces a native Process.apply SIGSEGV on {native_engine} for this GDS/boundary combination; the measured failure also occurs on CPU_DISK and GPU_DISK")
                    if kind == "simulate":
                        issues.append(FieldIssue(f"steps[{i}].nativeModel", message))
                    else:
                        warnings.append(f"steps[{i}].nativeModel: {message}")
                    continue
                signature = gpu_project_signature(normalized, step_dict)
                if step_value(step, "enabled") and (native_engine == "unavailable" or
                        (not native_engine.startswith("GPU_") and not capability_verified(
                            project.dimension, model_key, step_value(step, "operation"), boundary,
                            native_engine, schema_version=project.schemaVersion, **signature))):
                    if (project.solver.enginePreference == "gpu"
                            and normalized["solver"].get("gpuEngine") == "GPU_TRIANGLE"):
                        issues.append(FieldIssue(
                            f"steps[{i}].nativeModel",
                            f"GPU_TRIANGLE has no direct-reference acceptance row for model "
                            f"{model_key}, operation {step_value(step,'operation')}, boundary {boundary} "
                            f"in {project.dimension}D; the request is rejected instead of falling back to "
                            "GPU_DISK or CPU_DISK. Use solver.gpuEngine=GPU_DISK with a covered "
                            "combination, or select CPU."))
                        continue
                    if installed_model and installed_model.get("experimental") and not native_engine.startswith("GPU_"):
                        # Experimental models have native smoke tests but no
                        # archived direct-reference row yet. Only CPU_DISK (or
                        # the no-flux "not-applicable" path) is admitted, and
                        # the limitation is already reported as a warning.
                        continue
                    issues.append(FieldIssue(f"steps[{i}].nativeModel", f"this native model/dimension/operation/boundary {native_engine} combination has not passed direct-reference acceptance"))
                continue
            if step_value(step,"enabled") and (uses_flux(step) if project.schemaVersion in (4,5,6,7) else step_value(step,"model")=="singleParticle" and step_value(step,"rateMagnitude")>0):
                engine=select_engine(normalized,step_dict)
                if (step_value(step, "model") == "multiParticle" and engine.startswith("GPU_")
                        and sum(1 for row in (step_dict.get("species") or []) if row.get("kind") == "ion") > 1):
                    # Stock 4.7.0 drops every ion after the first without an
                    # error, so a multi-ion GPU step must be rejected here
                    # instead of silently producing a wrong geometry.
                    policy = multi_ion_gpu_error()
                    if policy is not None:
                        issues.append(FieldIssue(f"steps[{i}].species", policy))
                        continue
                signature = gpu_project_signature(normalized, step_dict)
                if engine=="unavailable" or (not engine.startswith("GPU_") and not capability_verified(
                        project.dimension,step_value(step,"model"),step_value(step,"operation"),boundary,engine,
                        schema_version=project.schemaVersion, **signature)):
                    if (project.solver.enginePreference == "gpu"
                            and normalized["solver"].get("gpuEngine") == "GPU_TRIANGLE"):
                        issues.append(FieldIssue(
                            f"steps[{i}].model",
                            f"GPU_TRIANGLE has no direct-reference acceptance row for model "
                            f"{step_value(step,'model')}, operation {step_value(step,'operation')}, boundary "
                            f"{boundary} in {project.dimension}D; the request is rejected instead of falling "
                            "back to GPU_DISK or CPU_DISK. Use solver.gpuEngine=GPU_DISK with a covered "
                            "combination, or select CPU."))
                        continue
                    limitation = known_engine_limitation(project.dimension, step_value(step, "model"),
                                                         "GPU_DISK", step_dict)
                    if limitation and project.solver.enginePreference == "gpu":
                        message = (f"installed ViennaPS {limitation['engineVersion']} GPU MultiParticle binding only converts {limitation['maxIonSpecies']} ion particle type; additional ion species are ignored. The Workbench split route uses representative solver coverage and still requires an accepted geometry/material class and supported species/rate callback signature. CPU_DISK remains available for this configuration")
                        if kind == "simulate":
                            issues.append(FieldIssue(f"steps[{i}].species", message))
                        else:
                            warnings.append(f"steps[{i}].species: {message}")
                    else:
                        issues.append(FieldIssue(f"steps[{i}].model","no verified engine combination for this schema, operation and boundary"))
        if issues:
            raise ProjectInputError(issues)
        return project, warnings
    if project.dimension == 3:
        return validate_3d(project, kind=kind)
    if project.solver.cpuThreads > available_cpu_threads():
        issues.append(FieldIssue("solver.cpuThreads", "thread count exceeds available CPU threads"))

    trench = project.geometry if project.geometry.kind == "trench" else None
    if trench:
        bottom = trench.width - 2.0 * trench.depth * math.tan(math.radians(trench.taperAngle))
        mask_half_spread = trench.maskHeight * abs(math.tan(math.radians(trench.maskTaperAngle)))
        narrowest_mask_opening = trench.width - 2.0 * mask_half_spread
        widest_mask_opening = trench.width + 2.0 * mask_half_spread
        if bottom <= 0:
            issues.append(FieldIssue("geometry.taperAngle", "taper produces a non-positive trench bottom width"))
        elif bottom < 2 * delta:
            issues.append(FieldIssue("geometry.taperAngle", "tapered bottom must span at least two grid intervals"))
        elif bottom < 5 * delta:
            warnings.append("Tapered trench bottom is represented by fewer than five grid intervals.")
        if trench.maskHeight > 0 and narrowest_mask_opening <= 0:
            issues.append(FieldIssue("geometry.maskTaperAngle", "mask taper closes or self-intersects the trench opening"))
        if trench.width >= extent or bottom >= extent or (trench.maskHeight > 0 and widest_mask_opening >= extent):
            issues.append(FieldIssue("geometry.width", "trench opening and bottom must fit within the horizontal domain"))
        if trench.maskHeight > 0 and 0 < narrowest_mask_opening < 5 * delta:
            warnings.append("The narrow mask opening spans fewer than five grid intervals.")
        features = [("geometry.width", trench.width), ("geometry.depth", trench.depth)]
        if trench.maskHeight > 0:
            features.append(("geometry.maskHeight", trench.maskHeight))
        for path, size in features:
            cells = size / delta
            if cells < 2:
                issues.append(FieldIssue(path, "positive geometry features must span at least two grid intervals"))
            elif cells < 5:
                warnings.append(f"{path} spans fewer than five grid intervals; geometric precision may be limited.")

    if issues:
        raise ProjectInputError(issues)
    return project, warnings


def _planarize_warnings(project: Any) -> list[str]:
    """Explain what a material-aware planarize can and cannot do.

    The installed ViennaPS Planarize accepts a height only. A
    ``stopMaterial`` step therefore derives one global cutoff from the
    area-weighted 90th percentile of that material's up-facing surface; it is
    not a CMP selectivity model. Surface the limitation in the same place the
    UI shows validation warnings.
    """
    warnings: list[str] = []
    for index, step in enumerate(project.steps):
        if not is_geometry_step(step) or step_value(step, "model") != "planarize":
            continue
        stop_material = step_value(step, "stopMaterial")
        if stop_material is None:
            continue
        warnings.append(
            f"steps[{index}]: material-aware CMP derives one global cutoff from the area-weighted 90th "
            f"percentile of {stop_material}'s up-facing surface, minus overPolish. ViennaPS {engine_version()} Planarize "
            f"still cuts every material above that height and models no lateral selectivity, so a stop material "
            f"whose upper plateau covers 10% of its exposed area or less, or that is genuinely multi-level "
            f"without one dominant upper plateau, can be over- or under-cut. The measured stop height, the "
            f"percentile and the effective cutoff are recorded in the step result.")
    return warnings


def _specialized_model_warnings(project: Any) -> list[str]:
    """State native semantics that the specialized UI fields cannot change.

    Two models are exposed ahead of their Stage 8/9 direct-reference evidence,
    and their 4.6.2 native behaviour differs from an ordinary deposition model.
    Keep those differences visible in the same warning list the UI renders.
    """
    warnings: list[str] = []
    for index, step in enumerate(project.steps):
        if step_value(step, "model") != "specialized":
            continue
        model_id = str(step_value(step, "nativeModel", ""))
        spec = model_spec(model_id)
        if spec is None:
            continue
        if spec.get("experimental"):
            warnings.append(
                f"steps[{index}].nativeModel: {model_id} has real native construction and CPU smoke coverage "
                f"in tests/test_engine_batch1_specialized.py, but no archived Stage 8/9 direct-reference "
                f"acceptance row. CPU_DISK is allowed with this warning; GPU coverage is not claimed.")
        if model_id == "faradayCage":
            domain = project.domain
            boundary = getattr(domain, "boundaryXY", None) or getattr(domain, "xBoundary", None)
            if boundary != "periodic":
                warnings.append(
                    f"steps[{index}].nativeModel: ViennaPS {engine_version()} FaradayCageEtching builds a periodic cage source "
                    f"and requires periodic lateral boundaries; this project uses {boundary!r}. The native run "
                    f"still executes but does not represent a periodic Faraday cage.")
            if project.dimension == 2:
                # 2D is newly exposed. A negative tiltAngle is accepted and is
                # NOT a no-op for small values: cage 45 / tilt -0.2 still
                # lowered the surface (0.8 -> 0.782019 um in the probe). The
                # etch depth falls toward zero as the tilt becomes more
                # negative (tilt -30 produced no measurable etch at the same
                # settings), so warn about the asymmetry without claiming an
                # unchanged surface. 3D keeps its existing response.
                tilt = step_value(step_value(step, "nativeParameters", {}), "tiltAngle", None)
                if isinstance(tilt, (int, float)) and float(tilt) < 0.0:
                    warnings.append(
                        f"steps[{index}].nativeParameters.tiltAngle: the installed native FaradayCageEtching "
                        f"accepts a negative tiltAngle, but the cage source is not symmetric between positive "
                        f"and negative tilt. A small negative tilt can still etch (tiltAngle -0.2 deg with "
                        f"cageAngle 45 deg lowered the 2D surface), and the etch depth falls toward zero as the "
                        f"tilt becomes more negative. Do not assume the surface stays flat.")
        if model_id == "geometricTrenchDeposition":
            warnings.append(
                f"steps[{index}].nativeModel: GeometricTrenchDeposition is a ViennaLS geometric-advection "
                f"distribution, not a flux or velocity-field deposition. It grows the step's depositMaterial "
                f"top level set without a native mask; {engine_version()} ignores trenchWidth, the process duration does not "
                f"scale the thickness, and the local radius a*(1-|z|/trenchDepth)^n+b is evaluated from absolute "
                f"z with the trench top assumed at z=0.")
        surface_diffusion = step_value(step, "surfaceDiffusion")
        if (model_id == "ald" and surface_diffusion is not None
                and step_value(surface_diffusion, "enabled", True)
                and ald_surface_diffusion_supported()):
            warnings.append(
                f"steps[{index}].surfaceDiffusion: ViennaPS {engine_version()} builds this Laplacian stencil "
                f"only inside the ALP strategy and only while coverageDiffusionCoefficient > 0. It smooths the "
                f"native Coverage scalar that drives the ALD growth rate; it does not smooth the level-set surface.")
    return warnings


def _directional_rate_set_warnings(project: Any) -> list[str]:
    """Warn when an explicit per-set mask list drops geometry protection.

    A ``rateSets`` entry without ``maskMaterials`` inherits the project's
    geometry-derived masks. An explicit list is honoured verbatim, including an
    empty list, so a set can act on a mask material. That is a deliberate
    choice, but it must never happen silently: surface it at validation time.
    """
    warnings: list[str] = []
    geometry_masks = geometry_mask_material_names(project.geometry)
    if not geometry_masks:
        return warnings
    for index, step in enumerate(project.steps):
        if step_value(step, "model") != "directional" or step_value(step, "rateSets") is None:
            continue
        for set_index, rate_set in enumerate(step_value(step, "rateSets")):
            masks = step_value(rate_set, "maskMaterials")
            if masks is None:
                continue
            missing = [name for name in geometry_masks if name not in masks]
            if missing:
                warnings.append(
                    f"steps[{index}].rateSets[{set_index}].maskMaterials explicitly omits geometry mask "
                    f"material(s) {', '.join(missing)}; this rate set moves those materials. Add them to the "
                    "list to keep the geometry protection.")
    return warnings


def _validate_isotropic_dispatch(project: Any) -> list[FieldIssue]:
    """Reject the geometric isotropic configurations the native path cannot do safely.

    ``isotropicMethod`` and ``isotropicTimeStepRatio`` are schema 4+ fields. The
    model already guarantees they only appear on isotropic steps and that an
    explicit ``geometric`` never carries ``rateMode=material``. What needs the
    whole project is the geometric safety envelope (native ``SphereDistribution``
    followed by one geometric advection):

    * one uniform offset only, so per-material rates are invalid;
    * the offset must span at least one grid interval or the sparse level set
      does not resolve it;
    * a positive (deposition) geometric advection cannot be masked, so a
      deposition with geometry mask materials is rejected while an etch may use
      ``addMaskMaterial``;
    * ``auto`` is always admitted: the worker resolves it to ``levelSet`` unless
      the configuration is both safe and faster (2D, uniform, at least one grid
      interval thick, no masked deposition).
    """
    issues: list[FieldIssue] = []
    if getattr(project, "schemaVersion", 0) not in (4, 5, 6, 7):
        return issues
    delta = float(project.domain.gridDelta)
    geometry_masks = geometry_mask_material_names(project.geometry)
    for index, step in enumerate(project.steps):
        if step_value(step, "model") != "isotropic":
            continue
        if step_value(step, "isotropicMethod") != "geometric":
            continue
        if (step_value(step, "rateMode", "uniform") != "uniform"
                or step_value(step, "materialRates") is not None):
            issues.append(FieldIssue(
                f"steps[{index}].isotropicMethod",
                "geometric isotropic advection applies one uniform offset and cannot carry a "
                "per-material rate table; use rateMode uniform or isotropicMethod levelSet"))
        thickness = float(step_value(step, "rateMagnitude")) * float(step_value(step, "duration"))
        if thickness < delta - 1e-12:
            issues.append(FieldIssue(
                f"steps[{index}].isotropicMethod",
                f"geometric thickness rateMagnitude * duration = {thickness:g} um is below one grid "
                f"interval ({delta:g} um); SphereDistribution cannot resolve a sub-grid offset. "
                "Increase the rate/duration or use isotropicMethod levelSet"))
        if step_value(step, "operation") == "deposit" and geometry_masks:
            issues.append(FieldIssue(
                f"steps[{index}].isotropicMethod",
                "geometric deposition cannot protect mask materials (a positive masked geometric "
                f"advection is unsafe), but the geometry defines mask material(s) "
                f"{', '.join(geometry_masks)}; use isotropicMethod levelSet or remove the masks"))
    return issues


def _isotropic_dispatch_warnings(project: Any) -> list[str]:
    """State which path ``isotropicMethod`` will take and what it costs."""
    warnings: list[str] = []
    if getattr(project, "schemaVersion", 0) not in (4, 5, 6, 7):
        return warnings
    for index, step in enumerate(project.steps):
        if step_value(step, "model") != "isotropic":
            continue
        method = step_value(step, "isotropicMethod")
        ratio = step_value(step, "isotropicTimeStepRatio")
        if method == "geometric":
            if project.dimension == 3:
                warnings.append(
                    f"steps[{index}].isotropicMethod: explicit geometric in 3D is allowed but usually "
                    "3-30x slower than the level-set IsotropicProcess for the same offset (including "
                    "plane geometry). Keep it only as a deliberate override.")
            if ratio is not None:
                warnings.append(
                    f"steps[{index}].isotropicTimeStepRatio is ignored for geometric: the native "
                    "SphereDistribution applies one geometric advection and runs no level-set PDE.")
        elif method == "auto":
            if project.dimension == 3:
                warnings.append(
                    f"steps[{index}].isotropicMethod: auto in 3D resolves to levelSet, because the "
                    "measured 3D geometric path is much slower and never worth selecting silently.")
            elif step_value(step, "rateMode", "uniform") != "uniform":
                warnings.append(
                    f"steps[{index}].isotropicMethod: auto resolves to levelSet because a material "
                    "rate table cannot be expressed by a single geometric offset.")
    return warnings


def _validate_particle_shape_steps(project: Any) -> list[FieldIssue]:
    """Reject the native particle-shape configurations that are not safe.

    ``BoxDistribution`` / ``SphereDistribution`` are ViennaLS geometric
    advection distributions. Measured 4.6.2/4.7.0 behaviour (one bounded
    subprocess per case, see docs/particle-shape-distribution-contract.md):

    * an active half-axis or radius of exactly 0 SIGSEGVs;
    * an active magnitude below one grid interval logs "smaller than the grid
      Delta" and can break the distribution;
    * a masked etch through ``addMaskMaterial`` is the officially supported
      combination and is used for ``operation=etch``;
    * any masked deposition SIGSEGVs in 2D and 3D, so a deposition never gets a
      mask (the project's mask materials are reported as omitted in the run);
    * mixed-sign half-axes can delete the surface or SIGSEGV, so the sign comes
      from ``operation`` and every native axis is positive.
    """
    issues: list[FieldIssue] = []
    delta = float(project.domain.gridDelta)
    boundary = getattr(project.domain, "boundaryXY", None) or getattr(project.domain, "xBoundary", None)
    for index, step in enumerate(project.steps):
        if step_value(step, "model") != "particleShape":
            continue
        if boundary != "reflective":
            issues.append(FieldIssue(
                f"steps[{index}].model",
                "particleShape uses ViennaLS GeometricAdvect, whose periodic-boundary support is "
                "still a native TODO; use reflective lateral boundaries for this step"))
        if step_value(step, "shape") == "box":
            values = step_value(step, "halfAxes") or []
            path = f"steps[{index}].halfAxes"
        else:
            values = [step_value(step, "radius")]
            path = f"steps[{index}].radius"
        for axis_index, value in enumerate(values):
            axis_path = f"{path}[{axis_index}]" if step_value(step, "shape") == "box" else path
            if float(value) < delta - 1e-12:
                issues.append(FieldIssue(
                    axis_path,
                    f"must be at least one grid interval ({delta:g} um); a half-axis or radius below "
                    "gridDelta triggers the native 'smaller than the grid Delta' warning and is not "
                    "resolved by the level set"))
    return issues


def _particle_shape_warnings(project: Any) -> list[str]:
    """State what a particleShape step is and is not in native 4.6.2.

    The native object is a geometric advection distribution: it offsets the
    exposed surface by exactly one radius / half-axis set and ignores the
    process duration. Etches protect the project's native mask materials
    through ``addMaskMaterial``; depositions cannot, because the native positive
    masked path SIGSEGVs, so their mask materials are omitted and recorded.
    """
    warnings: list[str] = []
    for index, step in enumerate(project.steps):
        if step_value(step, "model") != "particleShape":
            continue
        shape = "BoxDistribution" if step_value(step, "shape") == "box" else "SphereDistribution"
        if step_value(step, "operation") == "etch":
            mask_note = ("刻蚀会把项目几何的原生掩膜材料通过 addMaskMaterial 传给几何推进，"
                         "被保护的材料不动；掩膜覆盖整个暴露表面时原生会静默不动，工作台会因此报错。")
        else:
            mask_note = ("原生 4.6.2/4.7.0 的正向几何推进在有掩膜时 2D/3D 都会 SIGSEGV，"
                         "因此沉积步骤始终使用无掩膜重载：项目里的掩膜材料也会一起长大。")
        warnings.append(
            f"steps[{index}]: particleShape 使用原生 viennaps.{shape}（ViennaLS 几何推进），"
            "对当前整个暴露表面做一次形态学膨胀/收缩，位移等于半径/半轴，原生 Process 忽略持续时间。"
            f"它不是通量或速度刻蚀/沉积模型；{mask_note}"
            "（详见 docs/particle-shape-distribution-contract.md）。")
    return warnings


def _gds_boolean_warnings(project: Any) -> list[str]:
    """State what a GDS boolean does not model.

    The operand is a straight extrusion of the selected polygons, applied by the
    native ``Domain.applyBooleanOperation``. That is real mask geometry, but it
    is not lithography: no resist, no optical proximity/bias, no sidewall angle,
    and no material selectivity. Callers should not read it as a patterned etch.
    """
    warnings: list[str] = []
    for index, step in enumerate(project.steps):
        if not is_geometry_step(step) or step_value(step, "model") != "gdsBoolean":
            continue
        mask = step_value(step, "gdsMask") or {}
        warnings.append(
            f"steps[{index}]: GDS 布尔步骤使用图层 {step_value(mask, 'layer')}/{step_value(mask, 'datatype', 0)} 的"
            f"多边形竖直拉伸体作为原生布尔操作数（retainedRegion={step_value(mask, 'retainedRegion', 'inside')}）。"
            "它只改变几何：不包含光刻胶、光学邻近/偏置、侧壁角或材料选择比，也不等同于带掩膜的刻蚀。"
            "真正的接触孔/M0 图形化仍需后续选择性刻蚀或沉积步骤。")
    return warnings


def _material_table_issues(path: str, table: Any, names: set[str], available: set[str], *,
                           own_material: str | None = None,
                           zero_entries: set[str] | None = None) -> list[FieldIssue]:
    """Validate the material keys of a name-keyed rate table.

    ``materialRates`` is a JSON object, so duplicate names are impossible by
    construction and the schema already bounds every value to [0, 2]. What is
    left is catalog membership and whether the material exists when the step
    runs. ``own_material`` is the step's own deposition material, which
    ``duplicateTopLevelSet`` creates before the native process runs, so it is a
    legal table key even though it is absent at the step start.
    ``zero_entries`` names rows whose every rate is zero: an explicit zero row
    cannot move anything, so it stays valid even for a material that does not
    exist yet (the native call only needs its registered handle). A nonzero row
    for an absent material is still rejected.
    """
    zero_entries = zero_entries or set()
    issues: list[FieldIssue] = []
    for name in (table or {}):
        field_path = f"{path}.{name}"
        if name not in names:
            issues.append(FieldIssue(
                field_path,
                f"unknown or unavailable material {name!r}; choose a material from /api/materials"))
        elif name not in available and name != own_material and name not in zero_entries:
            issues.append(FieldIssue(
                field_path,
                f"material {name!r} is not present when this step runs; a material rate table can "
                "only address a material from the initial geometry or an earlier enabled deposition"))
    return issues


def _validate_materials(project: ProjectV3 | ProjectV4 | ProjectV5 | ProjectV6 | ProjectV7, *,
                        enforce_gds_domain: bool = False) -> list[FieldIssue]:
    try:
        from .materials import material_catalog
        catalog=material_catalog()
        names={row["name"] for row in catalog["materials"]}
    except (OSError,ValueError,KeyError,TypeError):
        return [FieldIssue("$", "verified material catalog is unavailable")]
    ids={int(row["id"]) for row in catalog["materials"]}
    if project.dimension==3:
        for value in project.view3D.visibleMaterialIds:
            if value not in ids: issues_view=[FieldIssue("view3D.visibleMaterialIds",f"unknown native material ID {value}")]; break
        else: issues_view=[]
        if any(key not in {str(value) for value in ids} for key in project.view3D.opacityByMaterial):
            issues_view.append(FieldIssue("view3D.opacityByMaterial","opacity keys must be native material IDs from /api/materials"))
    else: issues_view=[]
    g=project.geometry; roles: dict[str,set[str]]={}
    def add(name,role,path):
        if name not in names: issues.append(FieldIssue(path,f"unknown or unavailable material {name!r}; choose a material from /api/materials"))
        roles.setdefault(name,set()).add(role)
    issues=list(issues_view)
    output_settings = getattr(project, "outputSettings", None)
    cell_set = getattr(output_settings, "cellSet", None)
    if cell_set is not None and cell_set.enabled:
        cover = str(cell_set.coverMaterial)
        if cover != "GAS" and cover not in names:
            issues.append(FieldIssue(
                "outputSettings.cellSet.coverMaterial",
                f"unknown or unavailable material {cover!r}; choose GAS or a material from /api/materials"))
    available_by_step = _materials_available_by_step(project)
    uniform_protection=project.schemaVersion<4 or any(step_value(step,"rateMode","uniform")=="uniform" for step in project.steps)
    if g["kind"] == "gds":
        from . import storage
        from .gds import GdsFormatError, parse_gds
        try:
            source = storage.resolve_gds_asset(g["assetId"])
            library = parse_gds(source.read_bytes())
            available_layers = set(library.effective_layers(g["structure"]))
            selected_layers = {int(row["layer"]) for row in g["layers"]}
            if selected_layers - available_layers:
                issues.append(FieldIssue("geometry.layers", "selected layer IDs are not present in the selected GDS structure"))
            # A GDS boolean step re-reads this same asset, so its layer/datatype
            # must exist too. Mask operands also need closed polygons: an open
            # path would extrude to an empty body and fail at run time.
            available_pairs = set(library.effective_layer_datatypes(g["structure"]))
            mask_pairs = set(library.effective_mask_layer_datatypes(g["structure"]))
            # A GDS layer body is inserted on its layout grid. If the layout is
            # wider than the simulation domain, ViennaPS 4.6.2 does not clip it:
            # insertNextLevelSetAsMaterial crashes the worker with SIGSEGV. The
            # bounds below are pure Python and only cover direct elements; any
            # SREF/AREF layout returns None and keeps the native worker guard.
            blur = g.get("blur")
            blur_enabled = isinstance(blur, dict) and blur.get("enabled", True)
            padding_margin = 2.0 * float(project.domain.gridDelta) if blur_enabled else 0.0
            layout_bounds = (library.selected_layout_bounds_um(
                g["structure"], unit_scale=float(g["unitScale"]),
                position_um=g["position"][:2])
                if (enforce_gds_domain or blur_enabled) else None)
            if blur_enabled:
                # The installed blur allocates a fine exposure grid over the
                # *padded native layout*, not over the simulation domain. A 2D
                # layout can be thousands of micrometres tall while xExtent is
                # small, so budget both actual GDS axes.
                from .gds import MAX_GDS_BLUR_CELLS, blur_exposure_cell_count
                if layout_bounds is None:
                    issues.append(FieldIssue(
                        "geometry.blur",
                        "GDS blur cannot size its native exposure grid because the selected "
                        "structure has no provable XY layout bounds (SREF/AREF references or "
                        "invalid placement); flatten the layout or disable blur"))
                else:
                    grid_delta = float(project.domain.gridDelta)
                    exposure_delta = (float(blur.get("delta"))
                                      if blur.get("delta") is not None else grid_delta)
                    bounds_with_padding = (layout_bounds[0] - padding_margin,
                                           layout_bounds[1] - padding_margin,
                                           layout_bounds[2] + padding_margin,
                                           layout_bounds[3] + padding_margin)
                    cells = blur_exposure_cell_count(
                        bounds_with_padding, grid_delta=grid_delta,
                        exposure_delta=exposure_delta,
                        grid_refine=int(blur.get("gridRefine", 4)),
                        sigma_count=len(blur.get("sigmas") or []))
                    if cells > MAX_GDS_BLUR_CELLS:
                        width_x = bounds_with_padding[2] - bounds_with_padding[0]
                        width_y = bounds_with_padding[3] - bounds_with_padding[1]
                        issues.append(FieldIssue(
                            "geometry.blur",
                            "GDS blur exposure grid would allocate "
                            f"{cells:,} cells on the padded layout "
                            f"({width_x:g} x {width_y:g} um); the limit is "
                            f"{MAX_GDS_BLUR_CELLS:,} cells. Increase delta, reduce "
                            "gridRefine/sigmas, or shrink the layout"))
            if layout_bounds is not None and enforce_gds_domain:
                bounds = layout_bounds
                tolerance = max(1e-7, float(project.domain.gridDelta) * 1e-6)
                x_extent = float(project.domain.xExtent)
                if project.dimension == 3:
                    half_x = x_extent / 2.0 - padding_margin
                    half_y = float(project.domain.yExtent) / 2.0 - padding_margin
                    outside = (bounds[0] < -half_x - tolerance
                               or bounds[2] > half_x + tolerance
                               or bounds[1] < -half_y - tolerance
                               or bounds[3] > half_y + tolerance)
                else:
                    # ViennaPS builds the 2D Domain centered, but Stage 5 GDS
                    # layouts are authored in the absolute [0, xExtent] frame.
                    # Accept a layout that fits either frame entirely; blur
                    # padding shrinks both frames because the Python bounds are
                    # unpadded while the native read adds it.
                    absolute = (bounds[0] >= padding_margin - tolerance
                                and bounds[2] <= x_extent - padding_margin + tolerance)
                    centered = (bounds[0] >= -x_extent / 2.0 + padding_margin - tolerance
                                and bounds[2] <= x_extent / 2.0 - padding_margin + tolerance)
                    outside = not (absolute or centered)
                if outside:
                    margin_text = (f" and leave {padding_margin:g} um of native blur padding"
                                   if padding_margin else "")
                    issues.append(FieldIssue(
                        "geometry.assetId",
                        "GDS layout bounds "
                        f"[{bounds[0]:g}, {bounds[2]:g}]"
                        + (f" x [{bounds[1]:g}, {bounds[3]:g}]" if project.dimension == 3 else "")
                        + f" exceed the simulation domain (xExtent={x_extent:g}"
                        + ("; 2D accepts the absolute [0, xExtent] frame or the native "
                           "centered [-xExtent/2, xExtent/2] frame"
                           if project.dimension == 2 else "")
                        + (f", yExtent={float(project.domain.yExtent):g}" if project.dimension == 3 else "")
                        + f"); enlarge the domain or move the layout so it fits inside{margin_text}"))
            for step_index, step in enumerate(project.steps):
                if not is_geometry_step(step) or step_value(step, "model") != "gdsBoolean":
                    continue
                mask = step_value(step, "gdsMask") or {}
                pair = (int(step_value(mask, "layer")), int(step_value(mask, "datatype", 0)))
                if pair not in available_pairs:
                    issues.append(FieldIssue(
                        f"steps[{step_index}].gdsMask",
                        f"layer/datatype {pair[0]}/{pair[1]} is not present in GDS structure {g['structure']!r}"))
                elif pair not in mask_pairs:
                    issues.append(FieldIssue(
                        f"steps[{step_index}].gdsMask",
                        f"layer/datatype {pair[0]}/{pair[1]} contains no closed boundary polygons and cannot form a mask operand"))
        except FileNotFoundError:
            issues.append(FieldIssue("geometry.assetId", "GDS file is unavailable or its SHA-256 does not match; reimport the project package"))
        except (OSError, GdsFormatError, KeyError) as exc:
            issues.append(FieldIssue("geometry", f"GDS structure selection is invalid: {exc}"))
        for i, layer in enumerate(g["layers"]):
            role = "mask" if layer.get("mask") and uniform_protection else "film"
            add(layer["material"], role, f"geometry.layers[{i}].material")
    else:
        add(g["substrateMaterial"],"bulk","geometry.substrateMaterial")
        if g["kind"] == "stack" and g.get("gdsMask"):
            mask = g["gdsMask"]
            try:
                from . import storage
                from .gds import GdsFormatError, parse_gds
                library = parse_gds(storage.resolve_gds_asset(mask["assetId"]).read_bytes())
                if (int(mask["layer"]), int(mask["datatype"])) not in set(library.effective_layer_datatypes(mask["structure"])):
                    issues.append(FieldIssue("geometry.gdsMask", "selected GDS layer/datatype pair is not present in the selected structure"))
                elif (any(row.get("source") == "gds-mask" for row in g["layers"])
                      and (int(mask["layer"]), int(mask["datatype"])) not in set(library.effective_mask_layer_datatypes(mask["structure"]))):
                    issues.append(FieldIssue("geometry.gdsMask", "selected GDS layer/datatype pair contains no closed boundary polygons and cannot be used as a mask"))
            except (OSError, GdsFormatError, KeyError, ValueError) as exc:
                issues.append(FieldIssue("geometry.gdsMask", f"GDS mask asset or structure is invalid: {exc}"))
            add(mask["material"], "mask" if mask["protectsEtch"] and uniform_protection else "film", "geometry.gdsMask.material")
    if g["kind"] in ("trench","hole","fin"):
        if g.get("maskHeight",0)>0: add(g.get("maskMaterial","Mask"),"mask" if uniform_protection else "film","geometry.maskMaterial")
    if g["kind"]=="stack":
        for i,layer in enumerate(g["layers"]):
            if layer.get("source") == "gds-mask":
                continue
            add(layer["material"],layer["role"] if uniform_protection else "film",f"geometry.layers[{i}].material")
    for i,step in enumerate(project.steps):
        executed_names, planned_names = available_by_step[i]
        available_names = executed_names if step_value(step,"enabled") else planned_names
        # ``duplicateTopLevelSet`` creates this step's own deposition material
        # before the native process runs, so its table may address it even
        # though it does not exist at the step start.
        own_deposit = (step_value(step,"depositMaterial")
                       if step_value(step,"operation") == "deposit" else None)
        if step_value(step,"model") == "directional":
            if step_value(step,"rateSets") is not None:
                for set_index, rate_set in enumerate(step_value(step,"rateSets")):
                    for mask_index, name in enumerate(step_value(rate_set,"maskMaterials") or []):
                        if name not in names:
                            issues.append(FieldIssue(f"steps[{i}].rateSets[{set_index}].maskMaterials[{mask_index}]",
                                                     f"unknown or unavailable material {name!r}; choose a material from /api/materials"))
            else:
                directional_table = step_value(step,"materialRates") or {}
                directional_zero = {
                    name for name, row in directional_table.items()
                    if float(step_value(row,"directional",0.0)) <= 0.0
                    and float(step_value(row,"isotropic",0.0)) <= 0.0
                }
                issues.extend(_material_table_issues(
                    f"steps[{i}].materialRates", directional_table, names,
                    available_names, own_material=own_deposit,
                    zero_entries=directional_zero))
        if project.schemaVersion in (4,5,6,7) and step_value(step,"rateMode") == "material":
            isotropic_table = step_value(step,"materialRates") or {}
            isotropic_zero = {name for name, rate in isotropic_table.items() if float(rate) == 0.0}
            issues.extend(_material_table_issues(
                f"steps[{i}].materialRates", isotropic_table, names,
                available_names, own_material=own_deposit,
                zero_entries=isotropic_zero))
            reference=step_value(step,"selectivityReference")
            if reference is not None and reference not in names:
                issues.append(FieldIssue(f"steps[{i}].selectivityReference",f"unknown reference material {reference!r}"))
        if is_geometry_step(step) and step_value(step,"model")=="removeMaterial" and step_value(step,"material") not in names:
            issues.append(FieldIssue(f"steps[{i}].material",f"unknown or unavailable material {step_value(step,'material')!r}"))
        if is_geometry_step(step) and step_value(step,"model")=="insertBox":
            box_material = step_value(step,"material")
            if box_material not in names:
                issues.append(FieldIssue(f"steps[{i}].material",f"unknown or unavailable material {box_material!r}; choose a material from /api/materials"))
        if step_value(step,"model")=="particleShape":
            deposit = step_value(step,"depositMaterial")
            if deposit is not None and deposit not in names:
                issues.append(FieldIssue(
                    f"steps[{i}].depositMaterial",
                    f"unknown or unavailable material {deposit!r}; choose a material from /api/materials"))
            masks = step_value(step,"maskMaterials")
            if masks is not None:
                mask_mode = step_value(step,"maskMode") or "explicit"
                for mask_index, name in enumerate(masks):
                    if name not in names:
                        issues.append(FieldIssue(
                            f"steps[{i}].maskMaterials[{mask_index}]",
                            f"unknown or unavailable material {name!r}; choose a material from /api/materials"))
                    elif mask_mode == "explicit" and name not in available_names:
                        issues.append(FieldIssue(
                            f"steps[{i}].maskMaterials[{mask_index}]",
                            f"material {name!r} is not present when this step runs; an explicit particleShape "
                            "mask must come from the initial geometry or an earlier enabled deposition"))
        if is_geometry_step(step) and step_value(step,"model")=="planarize":
            cutoff_height = step_value(step,"cutoffHeight")
            stop_material = step_value(step,"stopMaterial")
            if cutoff_height is not None and abs(float(cutoff_height))/project.domain.gridDelta>1_000_000:
                issues.append(FieldIssue(f"steps[{i}].cutoffHeight","cutoff height exceeds the application coordinate budget"))
            if stop_material is not None:
                if stop_material not in names:
                    issues.append(FieldIssue(f"steps[{i}].stopMaterial",f"unknown or unavailable material {stop_material!r}; choose a material from /api/materials"))
                else:
                    # The CMP stop height is measured from the exposed surface of
                    # this material when the step runs, so it must exist by then.
                    if stop_material not in available_names:
                        issues.append(FieldIssue(f"steps[{i}].stopMaterial",
                            f"material {stop_material!r} is not present when this step runs; a stopMaterial CMP measures its exposed top, so it must come from the initial geometry or an earlier enabled deposition"))
        if is_geometry_step(step) and step_value(step,"model")=="boolean":
            position=step_value(step,"position"); width=float(step_value(step,"width")); height=float(step_value(step,"height")); depth=step_value(step,"depth")
            if width<2*project.domain.gridDelta-1e-12 or height<2*project.domain.gridDelta-1e-12 or (project.dimension==3 and float(depth)<2*project.domain.gridDelta-1e-12):
                issues.append(FieldIssue(f"steps[{i}]","boolean box dimensions must span at least two grid intervals"))
            if abs(float(position[0]))>project.domain.xExtent/2+width/2:
                issues.append(FieldIssue(f"steps[{i}].position","boolean box must intersect the x domain"))
            if project.dimension==3 and abs(float(position[1]))>project.domain.yExtent/2+float(depth)/2:
                issues.append(FieldIssue(f"steps[{i}].position","boolean box must intersect the y domain"))
            if any(abs(float(value))/project.domain.gridDelta>1_000_000 for value in position):
                issues.append(FieldIssue(f"steps[{i}].position","boolean position exceeds the application coordinate budget"))
        if step_value(step,"model")=="multiParticle":
            rate_table=step_value(step,"rateCoefficients") or {}
            for name in (rate_table.get("byMaterial",{}) if isinstance(rate_table,dict) else rate_table.byMaterial):
                if name not in names:
                    issues.append(FieldIssue(f"steps[{i}].rateCoefficients.byMaterial.{name}",f"unknown or unavailable material {name!r}"))
            for species_index,species in enumerate(step_value(step,"species",[]) or []):
                species_kind=step_value(species,"kind")
                if species_kind=="neutral":
                    sticking=step_value(species,"stickingProbabilityByMaterial",{}) or {}
                    for name in sticking:
                        if name not in names:
                            issues.append(FieldIssue(f"steps[{i}].species[{species_index}].stickingProbabilityByMaterial.{name}",f"unknown or unavailable material {name!r}"))
        if not is_geometry_step(step) and step_value(step,"operation")=="deposit":
            name=step_value(step,"depositMaterial")
            if name not in names: issues.append(FieldIssue(f"steps[{i}].depositMaterial", f"unknown or unavailable material {name!r}"))
            if step_value(step,"enabled") and has_motion(step):
                roles.setdefault(name,set()).add("film")
    for material,material_roles in roles.items():
        if "mask" in material_roles and len(material_roles)>1:
            issues.append(FieldIssue("geometry",f"material {material!r} is used as both mask and non-mask; native material IDs cannot distinguish these roles"))
    if any("mask" in roles.get(name,set()) and step_value(step,"rateMode","uniform")=="uniform" and step_value(step,"operation")=="deposit" and step_value(step,"depositMaterial")==name and step_value(step,"enabled") and has_motion(step) for name in roles for step in project.steps):
        issues.append(FieldIssue("steps", "nonzero deposition onto a protected mask material is unsupported by the current simplified model"))
    return issues


def _geometry_material_names(geometry: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    kind = geometry.get("kind")
    if kind == "gds":
        names.update(str(row["material"]) for row in geometry.get("layers", []))
    else:
        names.add(str(geometry.get("substrateMaterial", "")))
        if kind == "stack":
            names.update(str(row["material"]) for row in geometry.get("layers", []))
            if geometry.get("gdsMask"):
                names.add(str(geometry["gdsMask"].get("material", "Mask")))
        if kind in {"trench", "hole", "fin"} and float(geometry.get("maskHeight", 0)) > 0:
            names.add(str(geometry.get("maskMaterial", "Mask")))
    names.discard("")
    return names


def _materials_available_by_step(project: Any) -> list[tuple[set[str], set[str]]]:
    """Return the materials present when each step starts, in execution order.

    The first set is what actually exists: initial geometry materials plus the
    ``depositMaterial`` of **enabled** earlier deposition steps, mirroring the
    worker's ``duplicateTopLevelSet``. The second set is the same chain with
    disabled depositions included; a disabled step is an inactive draft, so its
    tables must not invalidate a project that is saved or previewed before the
    step (and its predecessors) are switched on.
    """
    initial = _geometry_material_names(project.geometry)
    executed = set(initial)
    planned = set(initial)
    per_step: list[tuple[set[str], set[str]]] = []
    for step in project.steps:
        per_step.append((set(executed), set(planned)))
        if step_value(step, "operation") == "deposit":
            name = step_value(step, "depositMaterial")
            if isinstance(name, str) and name:
                planned.add(name)
                if step_value(step, "enabled"):
                    executed.add(name)
        elif step_value(step, "model") == "insertBox":
            name = step_value(step, "material")
            if isinstance(name, str) and name:
                planned.add(name)
                if step_value(step, "enabled"):
                    executed.add(name)
    return per_step


def gpu_project_signature(project: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    """Return the geometry/material scope represented by a GPU evidence row."""
    geometry = project.get("geometry", {})
    names = _geometry_material_names(geometry)
    roles: dict[str, set[str]] = {}
    def add_role(name: Any, role: str) -> None:
        if name:
            roles.setdefault(str(name), set()).add(role)
    kind = geometry.get("kind")
    if kind == "gds":
        for layer in geometry.get("layers", []):
            add_role(layer.get("material"), "mask" if layer.get("mask") else "film")
    else:
        add_role(geometry.get("substrateMaterial"), "bulk")
        if kind == "stack":
            for layer in geometry.get("layers", []):
                add_role(layer.get("material"), str(layer.get("role", "film")))
            if geometry.get("gdsMask"):
                mask = geometry["gdsMask"]
                add_role(mask.get("material"), "mask" if mask.get("protectsEtch") else "film")
        elif kind in {"trench", "hole", "fin"} and float(geometry.get("maskHeight", 0)) > 0:
            add_role(geometry.get("maskMaterial", "Mask"), "mask")
    parameters = step.get("nativeParameters", {}) or {}
    for key in ("materialRates", "materialParametersByMaterial", "materialPlaneWaferRateByMaterial"):
        rows = parameters.get(key, {}) or {}
        if isinstance(rows, dict):
            names.update(str(name) for name in rows)
    rates = step.get("materialRates", {}) or {}
    if isinstance(rates, dict):
        names.update(str(name) for name in rates)
    relation = step.get("rateCoefficients", {}) or {}
    if isinstance(relation, dict):
        names.update(str(name) for name in (relation.get("byMaterial", {}) or {}))
    for species in step.get("species", []) or []:
        names.update(str(name) for name in (species.get("stickingProbabilityByMaterial", {}) or {}))
    bindings = {}
    if step.get("depositMaterial"):
        bindings["depositMaterial"] = str(step["depositMaterial"])
    for key in ("maskMaterial", "siliconMaterial", "oxideMaterial", "etchFrontMaterial"):
        value = parameters.get(key)
        if isinstance(value, str) and value:
            bindings[key] = value
    names.update(bindings.values())
    pattern = geometry.get("pattern", {}) if geometry.get("kind") == "stack" else {}
    species_signature = None
    if step.get("model") == "multiParticle":
        # GPU evidence must cover the exact installed particle combination.
        # Species count/kind and their model parameters change native process
        # setup, while the rate tables and sticking map change its callback.
        species_signature = {
            "species": step.get("species", []),
            "rateCoefficients": step.get("rateCoefficients", {}),
        }
    solver = project.get("solver", {})
    domain = project.get("domain", {})
    solver_profile = {
        "gridDelta": float(domain["gridDelta"]),
        "timeStepRatio": float(solver["timeStepRatio"]),
        "raysPerPoint": int(solver["raysPerPoint"]),
        "randomMode": str((solver.get("random") or {}).get("mode", "fixed")),
        "advection": solver.get("advection"),
        "rayTracing": solver.get("rayTracing"),
    }
    return {
        "geometry_kind": str(geometry.get("kind", "")),
        "pattern_kind": str(pattern.get("kind", "none")) if geometry.get("kind") == "stack" else None,
        "fin_extrusion_mode": ("extruded" if geometry.get("extrusion") is not None else "direct")
        if geometry.get("kind") == "fin" else None,
        "fin_construction": fin_construction_decision(project)["effective"] if geometry.get("kind") == "fin" else None,
        "gds_mapping_count": len(geometry.get("layers", [])) if geometry.get("kind") == "gds" else None,
        "gds_has_3d_mask": (project.get("dimension") == 3 and any(
            bool(layer.get("mask", False)) for layer in geometry.get("layers", [])))
            if geometry.get("kind") == "gds" else False,
        "materials": sorted(names),
        "material_roles": {name: sorted(value) for name, value in sorted(roles.items())},
        "material_bindings": dict(sorted(bindings.items())),
        "species_signature": species_signature,
        "solver_profile": solver_profile,
    }




def _advanced_number_issue(path: str, field: dict[str, Any], value: Any) -> FieldIssue | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return FieldIssue(path, "must be a finite JSON number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return FieldIssue(path, "must be a finite JSON number")
    if not math.isfinite(number):
        return FieldIssue(path, "must be a finite JSON number")
    if not float(field["minimum"]) <= number <= float(field["maximum"]):
        return FieldIssue(path, f"must be between {field['minimum']} and {field['maximum']}")
    return None


def _validate_advanced_parameters(prefix: str, spec: dict[str, Any], advanced: Any,
                                  material_names: set[str]) -> list[FieldIssue]:
    """Strict, per-model allowlist for ``nativeParameters.advanced``.

    The schema is generated from introspected ViennaPS members, so an unknown
    key means the request cannot be represented by the installed parameter
    object. Reject it rather than silently dropping a physics change.
    """
    path = f"{prefix}.advanced"
    if not isinstance(advanced, dict):
        return [FieldIssue(path, "must be a JSON object")]
    blocks = spec["blocks"]
    scalars = {field["key"]: field for field in spec["scalars"]}
    maps = {row["key"]: row for row in spec["maps"]}
    rejected = spec.get("rejectedKeys", {})
    allowed = set(blocks) | set(scalars) | set(maps)
    issues: list[FieldIssue] = []
    for name in sorted(set(advanced) & set(rejected)):
        issues.append(FieldIssue(f"{path}.{name}", rejected[name]))
    if unknown := sorted(set(advanced) - allowed - set(rejected)):
        issues.append(FieldIssue(path, f"unknown advanced keys {unknown}; allowed keys are {sorted(allowed)}"))
    for name, value in advanced.items():
        if name in blocks:
            block_path = f"{path}.{name}"
            if not isinstance(value, dict):
                issues.append(FieldIssue(block_path, "must be a JSON object"))
                continue
            fields = {field["key"]: field for field in blocks[name]["fields"]}
            if unknown := sorted(set(value) - set(fields)):
                issues.append(FieldIssue(block_path, f"unknown {name} keys {unknown}; allowed keys are {sorted(fields)}"))
            for key, entry in value.items():
                if key not in fields:
                    continue
                if fields[key]["kind"] == "boolean":
                    if type(entry) is not bool:
                        issues.append(FieldIssue(f"{block_path}.{key}", "must be a JSON boolean"))
                elif issue := _advanced_number_issue(f"{block_path}.{key}", fields[key], entry):
                    issues.append(issue)
            if {"thetaRMin", "thetaRMax"} <= set(fields):
                theta_min = value.get("thetaRMin", next(f["default"] for f in fields.values() if f["key"] == "thetaRMin"))
                theta_max = value.get("thetaRMax", next(f["default"] for f in fields.values() if f["key"] == "thetaRMax"))
                if isinstance(theta_min, (int, float)) and not isinstance(theta_min, bool) and isinstance(theta_max, (int, float)) and not isinstance(theta_max, bool) and theta_min > theta_max:
                    issues.append(FieldIssue(f"{block_path}.thetaRMin",
                                             "must be less than or equal to thetaRMax"))
            if {"minAngle", "inflectAngle"} <= set(fields):
                min_angle = value.get("minAngle", next(f["default"] for f in fields.values() if f["key"] == "minAngle"))
                inflect = value.get("inflectAngle", next(f["default"] for f in fields.values() if f["key"] == "inflectAngle"))
                if isinstance(min_angle, (int, float)) and not isinstance(min_angle, bool) and isinstance(inflect, (int, float)) and not isinstance(inflect, bool) and min_angle > inflect:
                    issues.append(FieldIssue(f"{block_path}.minAngle",
                                             "must be less than or equal to inflectAngle"))
        elif name in scalars:
            field = scalars[name]
            if field["kind"] == "boolean":
                if type(value) is not bool:
                    issues.append(FieldIssue(f"{path}.{name}", "must be a JSON boolean"))
            elif issue := _advanced_number_issue(f"{path}.{name}", field, value):
                issues.append(issue)
        elif name in maps:
            map_path = f"{path}.{name}"
            if not isinstance(value, dict) or not value:
                issues.append(FieldIssue(map_path, "must be an object with default and/or byMaterial"))
                continue
            if unknown := sorted(set(value) - {"default", "byMaterial"}):
                issues.append(FieldIssue(map_path, f"unknown map keys {unknown}; allowed keys are ['byMaterial', 'default']"))
            if "default" in value:
                if issue := _advanced_number_issue(f"{map_path}.default", maps[name], value["default"]):
                    issues.append(issue)
            if "byMaterial" in value:
                rows = value["byMaterial"]
                if not isinstance(rows, dict):
                    issues.append(FieldIssue(f"{map_path}.byMaterial", "must be a material-keyed object"))
                else:
                    for material, entry in rows.items():
                        if material not in material_names:
                            issues.append(FieldIssue(
                                f"{map_path}.byMaterial.{material}",
                                f"unknown installed material {material!r}"))
                        if issue := _advanced_number_issue(f"{map_path}.byMaterial.{material}", maps[name], entry):
                            issues.append(issue)
    return issues


def _specialized_flux_engine(project: Any) -> str:
    """Resolve the schema-7 engine the supervisor will pin for a specialized step.

    Mirrors ``select_engine`` for schema 7: ``cpu`` pins CPU_DISK, ``gpu`` pins
    the requested GPU engine (or GPU_DISK), and ``auto`` keeps the historical
    GPU_DISK-first policy for flux models.
    """
    preference = project.solver.enginePreference
    if preference == "cpu":
        return "CPU_DISK"
    if preference == "gpu":
        return str(project.solver.gpuEngine or "GPU_DISK")
    return "GPU_DISK"


def _validate_specialized_steps(project: ProjectV7) -> list[FieldIssue]:
    issues: list[FieldIssue] = []
    material_names = {row["name"] for row in _catalog_names()}
    custom_names: set[str] = set()
    try:
        from .materials import material_catalog
        custom_names = {row["name"] for row in material_catalog()["materials"] if row.get("isCustom")}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    geometry_names = _geometry_material_names(project.geometry)
    available_by_step = _materials_available_by_step(project)
    for index, step in enumerate(project.steps):
        if step.model != "specialized":
            continue
        spec = model_spec(step.nativeModel)
        if spec is None:
            continue
        engine = engine_version()
        surface_diffusion = step.surfaceDiffusion
        if (step.enabled and step.nativeModel == "ald" and surface_diffusion is not None
                and surface_diffusion.enabled):
            gate_message = ald_surface_diffusion_error(engine)
            if gate_message is not None:
                issues.append(FieldIssue(f"steps[{index}].surfaceDiffusion", gate_message))
        advanced = step.advancedOxidation
        if step.enabled and step.nativeModel == "oxidation" and advanced is not None:
            fields = {field["key"]: field for field in oxidation_advanced_spec()["fields"]}
            for key, reason in OXIDATION_ADVANCED_RESTRICTED.items():
                if oxidation_advanced_supported(key, engine):
                    continue
                default = fields[key]["default"]
                if getattr(advanced, key) == default:
                    # Materialised defaults are behaviour no-ops and must stay
                    # replayable. Only an attempt to change the control is
                    # rejected; the worker never sends the removed setter.
                    continue
                issues.append(FieldIssue(
                    f"steps[{index}].advancedOxidation.{key}",
                    f"ViennaPS {engine}: {reason}. The worker would silently ignore "
                    f"{key}={getattr(advanced, key)!r}; use the ViennaPS 4.6.2 runtime or omit this override."))
        allowed_dimensions = spec.get("dimensions")
        if allowed_dimensions and project.dimension not in allowed_dimensions:
            available = ", ".join(f"{value}D" for value in allowed_dimensions)
            issues.append(FieldIssue(
                f"steps[{index}].nativeModel",
                f"{step.nativeModel} is only available in {available}; this project is {project.dimension}D"))
            continue
        if step.enabled and step.nativeModel == "faradayCage" and project.dimension == 2:
            # Shared with the worker (build_specialized_model) so a direct
            # worker input cannot reach the hanging AUTO/GPU path either.
            gate_message = faraday_cage_2d_engine_error(_specialized_flux_engine(project))
            if gate_message is not None:
                issues.append(FieldIssue("solver.enginePreference", gate_message))
        # Material tables describe the stack the process sees: the initial
        # geometry plus everything earlier enabled deposition steps added.
        # A disabled draft step is checked against the same chain with disabled
        # depositions included so previews and saved drafts stay valid.
        executed_names, planned_names = available_by_step[index]
        available_names = executed_names if step.enabled else planned_names
        prefix = f"steps[{index}].nativeParameters"
        params = step.nativeParameters
        if step.nativeModel == "geometricTrenchDeposition":
            # Shared with the worker: a direct worker input cannot bypass the
            # dimension/geometry/depth/reach checks.
            for suffix, message in geometric_trench_issues(project, step):
                issues.append(FieldIssue(f"steps[{index}].{suffix}", message))
        if step.nativeModel in IBE_ENERGY_MODELS:
            # Shared with the worker: thresholdEnergy >= meanEnergy reverses
            # the native IBE flux and turns an etch step into growth.
            for suffix, message in ibe_energy_issues(params):
                issues.append(FieldIssue(f"steps[{index}].{suffix}", message))
        for field in spec["fields"]:
            key = field["key"]
            path = f"{prefix}.{key}"
            value = params.get(key)
            kind = field["kind"]
            if step.nativeModel == "geometricTrenchDeposition":
                # geometric_trench_issues() validated every numeric argument.
                continue
            if kind == "number":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    issues.append(FieldIssue(path, "must be a finite JSON number"))
                elif not float(field["minimum"]) <= float(value) <= float(field["maximum"]):
                    issues.append(FieldIssue(path, f"must be between {field['minimum']} and {field['maximum']}"))
            elif kind == "boolean":
                if type(value) is not bool:
                    issues.append(FieldIssue(path, "must be a JSON boolean"))
            elif kind == "enum":
                allowed = {row["value"] for row in field["options"]}
                if value not in allowed:
                    issues.append(FieldIssue(path, f"must be one of {sorted(allowed)}"))
            elif kind in {"material", "materialOptional"}:
                if kind == "materialOptional" and value == "":
                    continue
                if not isinstance(value, str) or value not in material_names:
                    issues.append(FieldIssue(path, f"must name a material from the installed catalog: {value!r}"))
                elif kind == "material" and value not in available_names and step.nativeModel not in {"selectiveEpitaxy"}:
                    issues.append(FieldIssue(path, f"material {value!r} is not present in the geometry available at this step"))
            elif kind == "text":
                if not isinstance(value, str) or not value or len(value) > 80 or any(ord(char) < 32 for char in value):
                    issues.append(FieldIssue(path, "must be nonempty text of at most 80 printable characters"))
            elif kind == "vector3":
                if not isinstance(value, list) or len(value) != 3 or any(
                    isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) for item in value
                ):
                    issues.append(FieldIssue(path, "must contain exactly three finite numbers"))
            elif kind in {"materialRates", "fluorocarbonMaterials"}:
                if not isinstance(value, dict):
                    issues.append(FieldIssue(path, "must be a material-keyed table"))
                    continue
                for name, entry in value.items():
                    if name not in material_names and name != "Polymer":
                        issues.append(FieldIssue(f"{path}.{name}", f"unknown or unavailable material {name!r}"))
                    if kind == "materialRates":
                        if isinstance(entry, bool) or not isinstance(entry, (int, float)) or not math.isfinite(float(entry)) or not field["minimum"] <= float(entry) <= field["maximum"]:
                            issues.append(FieldIssue(f"{path}.{name}", "material rate must be finite and within the displayed range"))
                    else:
                        expected = set(field["defaults"])
                        if not isinstance(entry, dict) or set(entry) != expected:
                            issues.append(FieldIssue(f"{path}.{name}", f"must contain exactly {sorted(expected)}"))
                            continue
                        for subkey, subvalue in entry.items():
                            if isinstance(subvalue, bool) or not isinstance(subvalue, (int, float)) or not math.isfinite(float(subvalue)) or not field["minimum"] <= float(subvalue) <= field["maximum"]:
                                issues.append(FieldIssue(f"{path}.{name}.{subkey}", "must be a finite number within the displayed range"))

        optional_fields = {field["key"]: field for field in spec.get("optionalFields", [])}
        for key, field in optional_fields.items():
            if key not in params:
                continue
            value = params[key]
            path = f"{prefix}.{key}"
            kind = field["kind"]
            if kind == "nvFactors":
                for message in nv_factors_issues(value):
                    issues.append(FieldIssue(path, message))
            elif kind == "number":
                if issue := _advanced_number_issue(path, field, value):
                    issues.append(issue)

        if "advanced" in params:
            spec_advanced = spec.get("advanced")
            if spec_advanced is None:
                issues.append(FieldIssue(f"{prefix}.advanced",
                                         f"{step.nativeModel} does not expose advanced native parameters"))
            else:
                # The built-in plasma models have no verified custom-material
                # parameter rows; keep advanced maps on the installed catalog.
                issues.extend(_validate_advanced_parameters(
                    prefix, spec_advanced, params["advanced"], material_names - custom_names))

        if step.nativeModel in {"wetEtch", "selectiveEpitaxy"}:
            table = params.get("materialRates", {})
            required = available_names if step.nativeModel == "wetEtch" else {"Si", "SiGe"}
            if set(table) != required:
                issues.append(FieldIssue(f"{prefix}.materialRates", f"must explicitly configure exactly these native material rates: {sorted(required)}"))
            if step.nativeModel == "selectiveEpitaxy" and isinstance(table, dict) and float(table.get("SiGe", 0.0)) <= 0.0:
                issues.append(FieldIssue(f"{prefix}.materialRates.SiGe", "must be positive so the native top material is recognized as an epitaxy material"))
        if step.nativeModel == "fluorocarbon":
            required = available_names | {"Polymer", "Mask"}
            if set(params.get("materialParametersByMaterial", {})) != required:
                issues.append(FieldIssue(f"{prefix}.materialParametersByMaterial", f"native FluorocarbonEtching requires a parameter row for every material present at this step and Polymer: {sorted(required)}"))
        if step.nativeModel in {"ionBeam", "faradayCage"}:
            table = params.get("materialPlaneWaferRateByMaterial", {})
            if isinstance(table, dict):
                unavailable = sorted(set(table) - available_names)
                if unavailable:
                    issues.append(FieldIssue(f"{prefix}.materialPlaneWaferRateByMaterial", f"may only reference materials available at this step (initial geometry plus earlier enabled deposition steps); not present: {unavailable}"))
        if step.nativeModel in {"sf6o2", "cf4o2", "hbro2", "sf6c4f8"} and geometry_names & custom_names:
            issues.append(FieldIssue(f"steps[{index}].nativeModel", "the installed built-in plasma model has no verified parameters for custom materials"))
        if step.nativeModel == "oxideRegrowth" and not {"SiO2", "Si3N4"}.issubset(geometry_names):
            issues.append(FieldIssue(f"steps[{index}].nativeModel", "OxideRegrowth requires native SiO2 and Si3N4 materials in the geometry"))
        if step.nativeModel == "oxideRegrowth" and (project.geometry.get("kind") != "stack" or
                project.geometry.get("pattern", {}).get("kind") != "none"):
            issues.append(FieldIssue(
                f"steps[{index}].nativeModel",
                f"installed OxideRegrowth is verified only on a flat stack with top-layer pattern set to none; "
                f"patterned or non-stack domains can crash ViennaPS {engine_version()}"))
        if step.nativeModel == "selectiveEpitaxy" and custom_names & geometry_names:
            issues.append(FieldIssue(f"steps[{index}].nativeModel", "SelectiveEpitaxy has only been verified with installed native epitaxy materials"))
        surface_diffusion = getattr(step, "surfaceDiffusion", None)
        if (surface_diffusion is not None and surface_diffusion.enabled
                and float(surface_diffusion.radius) > 0.0):
            # The native radius query stores one sparse row per surface point;
            # a radius spanning the whole domain makes the stencil quadratic in
            # point count and can exhaust worker memory before the artifact
            # budget runs. Eight grid spacings keeps it a local smoother.
            limit = min(2.0, 8.0 * float(project.domain.gridDelta))
            if float(surface_diffusion.radius) > limit:
                issues.append(FieldIssue(
                    f"steps[{index}].surfaceDiffusion.radius",
                    f"must not exceed 8 grid intervals ({limit:g} um for gridDelta="
                    f"{float(project.domain.gridDelta):g}); use radius=0 with kNeighbors for a bounded stencil"))
    return issues


def _validate_implant_steps(project: ProjectV7) -> list[FieldIssue]:
    """Validate the 4.7.0 cell-set implant / anneal / analysis capability.

    Everything here is a pure-Python check: a rejected request never reaches a
    native constructor, so an unsupported runtime, a missing material or a
    label that no earlier step produces cannot turn into a worker crash.
    """
    from .implant_models import (
        CELL_SET_STEP_MODELS, cell_set_output_conflict,
        field_geometry_issues, field_sequence_issues, label_provenance_issues,
        numeric_guard_messages, produced_labels, step_cell_set, step_value,
    )

    issues: list[FieldIssue] = []
    steps = [step if isinstance(step, dict) else step.model_dump(mode="json", exclude_none=True)
             for step in project.steps]
    cell_set_steps = [(index, step) for index, step in enumerate(steps)
                      if step_value(step, "model") in CELL_SET_STEP_MODELS]
    if not cell_set_steps:
        return issues

    runtime = engine_version()
    if runtime != "4.7.0":
        for index, _step in cell_set_steps:
            issues.append(FieldIssue(
                f"steps[{index}].model",
                f"ViennaPS {runtime} does not provide the native IonImplantation / Anneal / "
                "NetDoping / SheetResistance classes; this step requires the ViennaPS 4.7.0 "
                "runtime. The request is rejected before a worker starts instead of failing "
                "inside the native layer"))
        return issues

    conflict = cell_set_output_conflict(project)
    if conflict is not None:
        issues.append(FieldIssue("outputSettings.cellSet", conflict))

    try:
        from .materials import material_catalog
        names = {row["name"] for row in material_catalog()["materials"]}
    except (OSError, ValueError, KeyError, TypeError):
        names = set()

    available: set[str] = {"Material", "FillingFraction"}
    for index, path, message in field_sequence_issues(
            steps, grid_delta=float(project.domain.gridDelta),
            domain_periodic=(project.domain.boundaryXY == "periodic" if project.dimension == 3
                             else project.domain.xBoundary == "periodic")):
        issues.append(FieldIssue(f"steps[{index}].{path}" if path else f"steps[{index}]", message))
    for index, path, message in field_geometry_issues(
            steps, geometry=project.geometry, dimension=int(project.dimension),
            grid_delta=float(project.domain.gridDelta),
            x_extent=float(project.domain.xExtent),
            y_extent=float(getattr(project.domain, "yExtent", 0.0)),
            from_domain_state=project.continuationState is not None):
        issues.append(FieldIssue(f"steps[{index}].{path}" if path else f"steps[{index}]", message))
    for index, step in enumerate(steps):
        if step_value(step, "model") not in CELL_SET_STEP_MODELS:
            continue
        enabled = bool(step_value(step, "enabled"))
        for message in numeric_guard_messages(step, dimension=project.dimension,
                                              grid_delta=float(project.domain.gridDelta)):
            issues.append(FieldIssue(f"steps[{index}]", message))
        settings = step_cell_set(step) or {}
        cover = str(settings.get("coverMaterial") or "GAS")
        if cover != "GAS" and names and cover not in names:
            issues.append(FieldIssue(
                f"steps[{index}].cellSet.coverMaterial",
                f"unknown or unavailable material {cover!r}; choose GAS or a material from /api/materials"))
        requested_labels = settings.get("scalarLabels")
        if isinstance(requested_labels, list):
            unknown = sorted(set(requested_labels) - (available | produced_labels(step)))
            if unknown:
                issues.append(FieldIssue(
                    f"steps[{index}].cellSet.scalarLabels",
                    "label(s) not present in this step's native cell set: " + ", ".join(unknown)))
        model = step_value(step, "model")
        if model == "implant":
            if names:
                for field_name, values in (
                        ("material", [step_value(step, "material")]),
                        ("maskMaterials", list(step_value(step, "maskMaterials") or [])),
                        ("screenMaterials", list(step_value(step, "screenMaterials") or []))):
                    for material in values:
                        if material not in names:
                            issues.append(FieldIssue(
                                f"steps[{index}].{field_name}",
                                f"unknown or unavailable material {material!r}; choose a material from /api/materials"))
        elif model == "anneal":
            if names:
                for field_name in ("diffusionMaterials", "blockingMaterials"):
                    for material in step_value(step, field_name) or []:
                        if material not in names:
                            issues.append(FieldIssue(
                                f"steps[{index}].{field_name}",
                                f"unknown or unavailable material {material!r}; choose a material from /api/materials"))
            species = step_value(step, "speciesLabel")
            if enabled and species not in available:
                issues.append(FieldIssue(
                    f"steps[{index}].speciesLabel",
                    f"{species!r} is not produced by an earlier implant step"))
        elif model == "analysis":
            net = step_value(step, "netDoping")
            if isinstance(net, dict):
                for name in [*(net.get("donorLabels") or []), *(net.get("acceptorLabels") or [])]:
                    if enabled and name not in available:
                        issues.append(FieldIssue(
                            f"steps[{index}].netDoping",
                            f"{name!r} is not produced by an earlier implant/anneal step"))
            for entry_index, entry in enumerate(step_value(step, "sheetResistance") or []):
                label = entry.get("concentrationLabel") if isinstance(entry, dict) else None
                if enabled and label not in available:
                    issues.append(FieldIssue(
                        f"steps[{index}].sheetResistance[{entry_index}]",
                        f"{label!r} is not produced by an earlier implant/anneal step"))
        if enabled:
            # A disabled step produces nothing, so it must not satisfy a later
            # step's label provenance check.
            available |= produced_labels(step)

    for index, message in label_provenance_issues(steps):
        issues.append(FieldIssue(f"steps[{index}]", message))
    return issues


def _catalog_names() -> list[dict[str, Any]]:
    from .materials import material_catalog
    return material_catalog()["materials"]


def _validate_csv_process_steps(project: ProjectV7) -> tuple[list[FieldIssue], list[str]]:
    """Resolve and re-check every uploaded rate table before a worker starts.

    The native engine only logs a warning and continues with a zero velocity
    field when ``RateGrid::loadFromCSV`` fails, and a non-canonical table (BOM,
    tabs, Unicode whitespace) can disagree with this parser or crash LINEAR.
    Every table is therefore re-canonicalized here; a stored asset that is not
    in canonical form is a hard 422 and must be re-uploaded.
    """
    from . import storage
    from .csv_assets import RateCsvError, canonicalize_rate_csv

    issues: list[FieldIssue] = []
    warnings: list[str] = []
    dimension = int(project.dimension)
    half_x = float(project.domain.xExtent) / 2.0
    half_y = float(getattr(project.domain, "yExtent", 0.0)) / 2.0 if dimension == 3 else 0.0
    for index, step in enumerate(project.steps):
        if step.model != "csvProcess":
            continue
        prefix = f"steps[{index}]"
        try:
            path = storage.resolve_csv_asset(step.assetId)
            metadata_path = storage.csv_asset_metadata_path(step.assetId)
            metadata = storage.read_json(metadata_path) if metadata_path.is_file() else {}
            stored_dimension = metadata.get("dimension")
            raw = path.read_bytes()
            canonical, summary = canonicalize_rate_csv(
                raw,
                stored_dimension if type(stored_dimension) is int and stored_dimension in (2, 3) else dimension,
            )
        except FileNotFoundError:
            issues.append(FieldIssue(
                f"{prefix}.assetId",
                "CSV rate asset is unavailable or its SHA-256 does not match; re-upload the table"))
            continue
        except (OSError, ValueError, RateCsvError) as exc:
            issues.append(FieldIssue(f"{prefix}.assetId", f"CSV rate asset is invalid: {exc}"))
            continue
        if canonical != raw:
            issues.append(FieldIssue(
                f"{prefix}.assetId",
                "stored CSV rate asset is not in the canonical ASCII/LF format required by the native reader; "
                "re-upload the table"))
            continue
        if summary["dimension"] != dimension:
            issues.append(FieldIssue(
                f"{prefix}.assetId",
                f"rate table was uploaded for {summary['dimension']}D ({','.join(summary['columns'])}); "
                f"this project is {dimension}D"))
            continue
        offset_x = float(step.offset[0])
        offset_y = float(step.offset[1])
        query_min_x = -half_x + offset_x
        query_max_x = half_x + offset_x
        if summary["x"]["min"] > query_min_x or summary["x"]["max"] < query_max_x:
            warnings.append(
                f"{prefix}.assetId: the table's x range [{summary['x']['min']:g}, {summary['x']['max']:g}] "
                f"does not cover the native lookup range [{query_min_x:g}, {query_max_x:g}] um "
                f"(domain [-{half_x:g}, {half_x:g}] plus offset {offset_x:g}); native LINEAR extrapolates "
                "beyond the last sample and IDW always weights the nearest samples")
        if dimension == 3:
            query_min_y = -half_y + offset_y
            query_max_y = half_y + offset_y
            if summary["y"]["min"] > query_min_y or summary["y"]["max"] < query_max_y:
                warnings.append(
                    f"{prefix}.assetId: the table's y range [{summary['y']['min']:g}, {summary['y']['max']:g}] "
                    f"does not cover the native lookup range [{query_min_y:g}, {query_max_y:g}] um "
                    f"(domain [-{half_y:g}, {half_y:g}] plus offset {offset_y:g})")
        if step.interpolationMode == "LINEAR" and not summary["linearCompatible"]:
            if dimension == 2:
                detail = "2D LINEAR requires at least two rows with strictly increasing x"
            else:
                detail = "3D LINEAR requires a complete rectangular x/y grid with at least two distinct x and y values"
            issues.append(FieldIssue(f"{prefix}.interpolationMode", detail))
        if step.interpolationMode == "IDW" and int(step.idwNeighbors or 0) > int(summary["rowCount"]):
            issues.append(FieldIssue(
                f"{prefix}.idwNeighbors",
                f"must not exceed the table's {summary['rowCount']} rows"))
    return issues, warnings


def _validate_v3_geometry(project: ProjectV3) -> list[FieldIssue]:
    g=project.geometry; d=project.domain; out=[]; delta=d.gridDelta
    def feature(value,path):
        if value < 2*delta-1e-12:
            out.append(FieldIssue(path,"positive features must span at least two grid intervals"))
    if project.dimension==2 and g["kind"]=="hole":
        return [FieldIssue("geometry.kind","hole is only supported in 3D")]
    if g["kind"]=="plane":
        if abs(g["height"])/delta>1_000_000:
            out.append(FieldIssue("geometry.height","height exceeds the application coordinate budget"))
        return out
    if g["kind"]=="fin":
        if g["width"] >= d.xExtent:
            out.append(FieldIssue("geometry.width", "fin width must fit inside the lateral domain"))
        return out
    if g["kind"]=="gds":
        for axis, value in enumerate(g["position"]):
            if abs(float(value))/delta>1_000_000:
                out.append(FieldIssue(f"geometry.position[{axis}]", "GDS placement exceeds the application coordinate budget"))
        return out
    if g["kind"]=="stack":
        if abs(g["baseHeight"])/delta>1_000_000:
            out.append(FieldIssue("geometry.baseHeight","base height exceeds the application coordinate budget"))
        for i,layer in enumerate(g["layers"]):
            feature(layer["thickness"],f"geometry.layers[{i}].thickness")
        pattern=g["pattern"]
        if pattern["kind"] in ("none", "gds"): return out
        if pattern["kind"]=="hole" and project.dimension==2:
            return out+[FieldIssue("geometry.pattern.kind","2D stacks do not support hole patterns")]
        if pattern.get("layers") is not None:
            if pattern["kind"] != "trench":
                return out+[FieldIssue("geometry.pattern.layers",
                    "per-layer cutouts are supported only for trench patterns")]
            by_id={layer["id"]:index for index,layer in enumerate(g["layers"])}
            for position, entry in enumerate(pattern["layers"]):
                index = entry.get("layerIndex")
                if index is None:
                    index = by_id.get(entry.get("layerId"))
                if index is None or not 0 <= int(index) < len(g["layers"]):
                    out.append(FieldIssue(
                        f"geometry.pattern.layers[{position}]",
                        "references an unknown stack layer"))
                    continue
                path=f"geometry.pattern.layers[{position}]"
                thickness=float(g["layers"][int(index)]["thickness"])
                # In the native per-layer constructor the cutout is anchored at
                # the layer bottom, so width is the top width and a positive
                # angle widens the bottom by 2*height*tan(angle).
                change=2.0*thickness*math.tan(math.radians(float(entry["taperAngle"])))
                narrow=float(entry["width"])+min(0.0,change)
                wide=float(entry["width"])+max(0.0,change)
                feature(float(entry["width"]),f"{path}.width")
                feature(narrow,f"{path}.taperAngle")
                if wide >= d.xExtent:
                    out.append(FieldIssue(path,
                        "per-layer cutout and tapered opening must fit inside the lateral domain"))
            return out
        size=pattern["width"] if pattern["kind"]=="trench" else 2*pattern["radius"]
        change=2*g["layers"][-1]["thickness"]*math.tan(math.radians(pattern["taperAngle"]))
        feature(min(size,size-change),"geometry.pattern")
        limit=d.xExtent if pattern["kind"]=="trench" else min(d.xExtent,d.yExtent)
        if max(size,size-change)>=limit:
            out.append(FieldIssue("geometry.pattern","pattern and tapered opening must fit inside the lateral domain"))
        return out
    size=g["width"] if g["kind"]=="trench" else 2*g["radius"]
    bottom=size-2*g["depth"]*math.tan(math.radians(g["taperAngle"]))
    mask_spread=2*g["maskHeight"]*abs(math.tan(math.radians(g["maskTaperAngle"])))
    feature(size,"geometry.width" if g["kind"]=="trench" else "geometry.radius")
    feature(g["depth"],"geometry.depth")
    feature(bottom,"geometry.taperAngle")
    if g["maskHeight"]>0:
        feature(g["maskHeight"],"geometry.maskHeight")
        feature(size-mask_spread,"geometry.maskTaperAngle")
    limit=d.xExtent if g["kind"]=="trench" else min(d.xExtent,d.yExtent)
    if max(size,bottom,size+mask_spread)>=limit:
        out.append(FieldIssue("geometry","structure must fit inside the lateral domain"))
    return out


def _validate_v3_2d_geometry(project: ProjectV3) -> list[FieldIssue]:
    if isinstance(project.domain,Domain3D): return [FieldIssue("domain","2D projects require the 2D domain")]
    return _validate_v3_geometry(project)


def _validate_v3_3d_geometry(project: ProjectV3) -> list[FieldIssue]:
    if not isinstance(project.domain,Domain3D): return [FieldIssue("domain","3D projects require the 3D domain")]
    return _validate_v3_geometry(project)


def _gpu_material_scope_matches(scope: dict[str, Any], materials: list[str] | None,
                               material_roles: dict[str, list[str]] | None,
                               material_bindings: dict[str, str] | None,
                               geometry_kind: str | None, operation: str,
                               boundary: str) -> bool:
    if (scope.get("kind") != "viennaps-native-material-registry-v1"
            or scope.get("engineVersion") != EVIDENCE_ENGINE_VERSION
            or boundary not in scope.get("boundaries", [])):
        return False
    if materials is None or material_roles is None or material_bindings is None or geometry_kind is None:
        return False
    allowed_roles = scope.get("roleKindsByGeometry", {}).get(geometry_kind, [])
    actual_roles = sorted({role for roles in material_roles.values() for role in roles})
    if actual_roles not in allowed_roles:
        return False
    expected_bindings = scope.get("bindingKeysByOperation", {}).get(operation)
    if expected_bindings is None or sorted(material_bindings) != sorted(expected_bindings):
        return False
    expected_binding_values = scope.get("bindingValuesByOperation", {})
    binding_roles = scope.get("bindingRolesByOperation", {}).get(operation, {})
    if operation in expected_binding_values:
        static_bindings = {key: value for key, value in material_bindings.items() if key not in binding_roles}
        if static_bindings != expected_binding_values[operation]:
            return False
    for key, role in binding_roles.items():
        if key not in material_bindings or not isinstance(role, str) or not role:
            return False
        role_names = {name for name, roles in material_roles.items() if role in roles}
        if material_bindings[key] not in role_names:
            return False
    catalog = _catalog_names()
    by_name = {str(row["name"]): row for row in catalog}
    builtin_ids = {int(value) for value in scope.get("builtInLegacyIds", [])}
    custom_range = scope.get("customLegacyIdRange", [])
    if (len(custom_range) != 2 or int(custom_range[0]) != 177 or int(custom_range[1]) != 240
            or not builtin_ids):
        return False
    intrinsic_materials = set(scope.get("intrinsicMaterials", []))
    seen_ids: set[int] = set()
    for name in materials:
        if name in intrinsic_materials:
            continue
        row = by_name.get(name)
        if row is None:
            return False
        native_id = int(row["id"])
        if native_id in seen_ids:
            return False
        seen_ids.add(native_id)
        if row.get("isCustom"):
            if not int(custom_range[0]) <= native_id <= int(custom_range[1]):
                return False
        elif native_id not in builtin_ids:
            return False
    if not set(material_roles).issubset(materials):
        return False
    if not set(material_bindings.values()).issubset(materials):
        return False
    allowed_sets = scope.get("allowedMaterialSets")
    if allowed_sets is not None:
        if (not isinstance(allowed_sets, list) or not allowed_sets
                or any(not isinstance(names, list) or not names or len(names) != len(set(names))
                       or any(not isinstance(name, str) or not name for name in names)
                       for names in allowed_sets)):
            return False
        allowed = {tuple(sorted(names)) for names in allowed_sets}
        if tuple(sorted(set(materials))) not in allowed:
            return False
    role_ids = scope.get("materialLegacyIdsByRoleByGeometry", {}).get(geometry_kind, {})
    for name, roles in material_roles.items():
        if not roles:
            return False
        row = by_name.get(name)
        if row is None:
            # Intrinsic ViennaPS process materials such as Polymer are not
            # selectable geometry materials and must not acquire a geometry role.
            if name in intrinsic_materials:
                return False
            continue
        native_id = int(row["id"])
        for role in roles:
            allowed_ids = role_ids.get(role)
            if allowed_ids is not None and native_id not in {int(value) for value in allowed_ids}:
                return False
    return True


def _gpu_species_table_scope_matches(baseline: dict[str, Any] | None,
                                     candidate: dict[str, Any] | None) -> bool:
    """Allow material-keyed MP tables to vary while keeping native species fixed."""
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        return False

    def normalize(signature: dict[str, Any]) -> dict[str, Any] | None:
        species = signature.get("species")
        relation = signature.get("rateCoefficients")
        if not isinstance(species, list) or not isinstance(relation, dict):
            return None
        labels = [item.get("label") for item in species if isinstance(item, dict)]
        if len(labels) != len(species) or not labels or len(labels) != len(set(labels)):
            return None
        defaults = relation.get("defaultBySpecies")
        overrides = relation.get("byMaterial", {})
        if not isinstance(defaults, dict) or set(defaults) != set(labels) or not isinstance(overrides, dict):
            return None
        normalized_species = []
        for item in species:
            if item.get("kind") == "ion":
                normalized_species.append({key: value for key, value in item.items() if key != "label"})
            elif item.get("kind") == "neutral":
                sticking = item.get("stickingProbabilityByMaterial", {})
                if not isinstance(sticking, dict) or any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(float(value)) or not 0 <= float(value) <= 1
                    for value in sticking.values()
                ):
                    return None
                normalized_species.append({key: value for key, value in item.items()
                                           if key not in {"label", "stickingProbabilityByMaterial"}})
            else:
                return None
        for row in overrides.values():
            if not isinstance(row, dict) or set(row) - set(labels):
                return None
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(float(value)) or not 0 <= float(value) <= 2
                   for value in row.values()):
                return None
        if any(isinstance(defaults[label], bool) or not isinstance(defaults[label], (int, float))
               or not math.isfinite(float(defaults[label])) or not 0 <= float(defaults[label]) <= 2
               for label in labels):
            return None
        return {"species": normalized_species,
                "defaultRatesByOrder": [float(defaults[label]) for label in labels]}

    left, right = normalize(baseline), normalize(candidate)
    return left is not None and left == right


def _gpu_combination_verified(dimension: int, model: str, operation: str, boundary: str,
                              engine: str, schema_version: int | None, *,
                              execution_strategy: str | None = None,
                              flux_strategy: str | None = None,
                              geometry_kind: str | None = None, pattern_kind: str | None = None,
                              fin_extrusion_mode: str | None = None,
                              fin_construction: str | None = None,
                              gds_mapping_count: int | None = None,
                              gds_has_3d_mask: bool = False,
                              materials: list[str] | None = None,
                              material_roles: dict[str, list[str]] | None = None,
                              material_bindings: dict[str, str] | None = None,
                              species_signature: dict[str, Any] | None = None,
                              solver_profile: dict[str, Any] | None = None) -> bool:
    path = Path(__file__).resolve().parents[1] / "docs" / "stage9-evidence" / "gpu-capability-matrix.json"
    try:
        data = json.loads(path.read_text())
        # Solver profiles in positive rows describe representative direct runs.
        # They do not define an exhaustive Cartesian product of CFL, ray count,
        # advection, and ray-tracing settings. Only profiles with measured
        # structural mismatches are held back in the explicit failure list.
        for failure in data.get("knownGpuProfileFailures", []):
            if not all(failure.get(key) == value for key, value in {
                "dimension": dimension, "model": model, "operation": operation,
                "boundaryXY": boundary, "engine": engine, "geometryKind": geometry_kind,
                "materials": materials, "materialRoles": material_roles,
                "materialBindings": material_bindings, "speciesSignature": species_signature,
            }.items()):
                continue
            failed_profile = failure.get("solverProfile", {})
            if failed_profile and solver_profile is not None and all(
                solver_profile.get(key) == value for key, value in failed_profile.items()
            ):
                return False
        for row in data.get("rows", []):
            if row.get("status") != "verified" or (schema_version is not None and schema_version not in row.get("schemaVersions", [])):
                continue
            if not all(row.get(key) == value for key, value in {
                "dimension": dimension, "model": model, "operation": operation,
                "boundaryXY": boundary, "engine": engine,
            }.items()):
                continue
            if execution_strategy is not None and row.get("executionStrategy") != execution_strategy:
                continue
            if flux_strategy is not None and row.get("singlePassFluxStrategy") != flux_strategy:
                continue
            # solverProfile is provenance for the representative run. Numerical
            # profile changes inherit the model/geometry/material capability
            # class, except for explicitly recorded profiles that failed direct
            # structural comparison above.
            # New schemas fail closed unless geometry and material scope match
            # the combination that was measured against direct ViennaPS.
            if schema_version not in (1, 2):
                if geometry_kind is None or geometry_kind not in row.get("geometryKinds", []):
                    continue
                if geometry_kind == "fin" and (
                    fin_extrusion_mode not in ("direct", "extruded")
                    or row.get("finExtrusionMode") != fin_extrusion_mode
                    or fin_construction not in ("native", "boolean")
                    or row.get("finConstruction") != fin_construction
                ):
                    continue
                if geometry_kind == "gds":
                    scope = row.get("gdsScope", {})
                    if (scope.get("kind") != "viennaps-gds-process-v1"
                            or scope.get("engineVersion") != EVIDENCE_ENGINE_VERSION
                            or dimension not in scope.get("dimensions", [])
                            or gds_mapping_count is None
                            or gds_mapping_count < 1
                            or gds_mapping_count > int(scope.get("maxLayerMappings", 0))
                            or (gds_has_3d_mask and not scope.get("supports3DMask", False))):
                        continue
                if row.get("patternKinds") and (pattern_kind is None or pattern_kind not in row["patternKinds"]):
                    continue
                material_scope = row.get("materialScope")
                if material_scope:
                    if not _gpu_material_scope_matches(material_scope, materials, material_roles,
                                                       material_bindings, geometry_kind, operation, boundary):
                        continue
                else:
                    if materials is None or sorted(set(materials)) != sorted(row.get("materials", [])):
                        continue
                    # Material roles and selected native material handles affect
                    # masking/deposition and must agree with the measured input.
                    if material_roles is None or row.get("materialRoles") != material_roles:
                        continue
                    if material_bindings is None or row.get("materialBindings") != material_bindings:
                        continue
                if model == "multiParticle":
                    if row.get("speciesScope", {}).get("kind") == "material-tables-v1":
                        if not _gpu_species_table_scope_matches(row.get("speciesSignature"), species_signature):
                            continue
                    elif species_signature is None or row.get("speciesSignature") != species_signature:
                        continue
            return True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # Keep legacy v1/v2 projects working from the host probes, while all newer
    # schemas require a Stage 9 exact-model matrix row.
    if schema_version == 1:
        return dimension == 2 and model == "singleParticle" and boundary == "reflective" and operation in ("etch", "deposit") and gpu_disk_verified()
    if schema_version == 2:
        try:
            data = json.loads((Path(__file__).resolve().parents[1] / "docs" / "capabilities-3d.json").read_text())
            return any(row.get("status") == "verified" and all(row.get(key) == value for key, value in {
                "dimension": dimension, "model": model, "operation": operation,
                "boundaryXY": boundary, "engine": engine,
            }.items()) for row in data.get("rows", []))
        except (OSError, ValueError, KeyError, TypeError):
            return False
    return False




def experimental_cpu_replay_allowed(step: Any, engine: str) -> bool:
    """Allow CPU replay for allowlisted experimental specialized models.

    These models have real native CPU smoke coverage but no archived Stage 8/9
    direct-reference row, so ``capability_verified`` is intentionally false.
    A run that the validator already admitted as experimental CPU must remain
    reproducible through its replay envelope; GPU replay is not granted.
    """
    if engine != "CPU_DISK" or not isinstance(step, dict) or step.get("model") != "specialized":
        return False
    spec = model_spec(str(step.get("nativeModel", "")))
    return bool(spec and spec.get("experimental"))

def capability_verified(dimension: int, model: str, operation: str, boundary: str, engine: str,
                        *, schema_version: int | None = None, execution_strategy: str | None = None,
                        flux_strategy: str | None = None,
                        geometry_kind: str | None = None,
                        pattern_kind: str | None = None, fin_extrusion_mode: str | None = None,
                        fin_construction: str | None = None, gds_mapping_count: int | None = None,
                        gds_has_3d_mask: bool = False,
                        materials: list[str] | None = None,
                        material_roles: dict[str, list[str]] | None = None,
                        material_bindings: dict[str, str] | None = None,
                        species_signature: dict[str, Any] | None = None,
                        solver_profile: dict[str, Any] | None = None) -> bool:
    if engine.startswith("GPU_"):
        return _gpu_combination_verified(dimension, model, operation, boundary, engine, schema_version,
                                         execution_strategy=execution_strategy,
                                         flux_strategy=flux_strategy,
                                         geometry_kind=geometry_kind, pattern_kind=pattern_kind, materials=materials,
                                         fin_extrusion_mode=fin_extrusion_mode,
                                         fin_construction=fin_construction,
                                         gds_mapping_count=gds_mapping_count,
                                         gds_has_3d_mask=gds_has_3d_mask,
                                         material_roles=material_roles, material_bindings=material_bindings,
                                         species_signature=species_signature,
                                         solver_profile=solver_profile)
    if model.startswith("specialized:"):
        try:
            data = json.loads((Path(__file__).resolve().parents[1] / "docs/stage8-evidence/capability-matrix.json").read_text())
            native_model = model.split(":", 1)[1]
            return any(row.get("status") == "verified" and all(row.get(key) == value for key, value in {
                "dimension": dimension, "nativeModel": native_model, "operation": operation,
                "boundaryXY": boundary, "engine": engine,
            }.items()) for row in data.get("rows", []))
        except (OSError, ValueError, KeyError, TypeError):
            return False
    if model == "multiParticle":
        try:
            data=json.loads((Path(__file__).resolve().parents[1]/"docs/stage7-evidence/capability-matrix.json").read_text())
            return any(row.get("status")=="verified" and all(row.get(key)==value for key,value in {
                "dimension":dimension,"model":model,"operation":operation,"boundaryXY":boundary,"engine":engine
            }.items()) for row in data.get("rows",[]))
        except (OSError,ValueError,KeyError,TypeError):
            return False
    if dimension == 2:
        return engine == "CPU_DISK"
    try:
        data = json.loads((Path(__file__).resolve().parents[1]/"docs/capabilities-3d.json").read_text())
        return any(row.get("status") == "verified" and all(row.get(k) == v for k,v in {"dimension":dimension,"model":model,"operation":operation,"boundaryXY":boundary,"engine":engine}.items()) for row in data["rows"])
    except (OSError,ValueError,KeyError,TypeError):
        return False


def slice_axis_bounds(project: dict[str, Any], slice_request: dict[str, Any]) -> dict[str, Any]:
    """Return the native Slice axis, output extent and source-axis bound.

    The slice position moves along the normal axis, so it is bounded by that
    axis' domain extent. Half-domain geometries (HALF/QUARTER holes and
    halfFin/halfTrench) only occupy the positive half of the halved axes.
    z has no finite computational boundary; its bound is the measured source
    surface range and is checked by the worker after the initial snapshot.
    """
    axis_label = str(slice_request.get("sliceAxis") or "y")
    if axis_label not in {"x", "y", "z"}:
        raise ValueError(f"native Slice axis must be x, y or z, not {axis_label!r}")
    axis = {"x": 0, "y": 1, "z": 2}[axis_label]
    domain = project["domain"]
    geometry = project.get("geometry") or {}
    hole_shape = str(geometry.get("holeShape") or "FULL")
    half_x = (hole_shape in ("HALF", "QUARTER")
              or bool(geometry.get("halfTrench")) or bool(geometry.get("halfFin")))
    half_y = hole_shape == "QUARTER"
    x_extent = float(domain["xExtent"])
    y_extent = float(domain["yExtent"])
    if axis == 0:
        return {"axis": axis, "axisName": "x", "outputExtent": y_extent,
                "min": 0.0 if half_x else -x_extent / 2,
                "max": x_extent / 2, "halfDomain": half_x,
                "restriction": "half-domain" if half_x else None,
                "lateralAxis": "y", "verticalAxis": "z", "normal": [1.0, 0.0, 0.0]}
    if axis == 1:
        if geometry.get("kind") == "fin" and geometry.get("extrusion") is not None:
            # An extruded 3D fin occupies only the requested y extrusion
            # range, regardless of whether the worker uses the d2-Extrude
            # route or the direct 3D box-stencil route.
            y_min, y_max = (float(value) for value in geometry["extrusion"]["extent"])
            restriction = "extrusion"
        elif half_y:
            y_min, y_max = 0.0, y_extent / 2
            restriction = "half-domain"
        else:
            y_min, y_max = -y_extent / 2, y_extent / 2
            restriction = None
        return {"axis": axis, "axisName": "y", "outputExtent": x_extent,
                "min": y_min, "max": y_max,
                "halfDomain": half_y,
                "restriction": restriction,
                "lateralAxis": "x", "verticalAxis": "z", "normal": [0.0, 1.0, 0.0]}
    return {"axis": axis, "axisName": "z", "outputExtent": x_extent,
            "min": None, "max": None, "halfDomain": False, "restriction": None,
            "lateralAxis": "x", "verticalAxis": "y", "normal": [0.0, 0.0, 1.0]}


def slice_position_issue(project: dict[str, Any], slice_request: dict[str, Any]) -> str | None:
    """Return a 422-ready reason when the Slice position misses its axis."""
    try:
        position = float(slice_request["position"])
    except (KeyError, TypeError, ValueError):
        return "native Slice requires a finite position"
    if not math.isfinite(position):
        return "native Slice requires a finite position"
    bounds = slice_axis_bounds(project, slice_request)
    if bounds["min"] is None:
        return None
    if bounds["min"] - 1e-12 <= position <= bounds["max"] + 1e-12:
        return None
    half_note = f" ({bounds['restriction']} limit)" if bounds.get("restriction") else ""
    return (f"native Slice {bounds['axisName']} position {position:g} lies outside the source "
            f"{bounds['axisName']} range [{bounds['min']:g}, {bounds['max']:g}]{half_note}")


def select_engine(project: dict, step: dict, *, analysis_mode: str = "process") -> str:
    if not (uses_flux(step) if project.get("schemaVersion") in (4,5,6,7) else step["model"] == "singleParticle" and step["rateMagnitude"] > 0):
        return "not-applicable"
    preference = project["solver"]["enginePreference"]
    model = step["model"]
    if model == "specialized":
        model = f"specialized:{step.get('nativeModel', '')}"
    if project.get("schemaVersion") in (3,4,5,6,7):
        requested_gpu_engine = project.get("solver", {}).get("gpuEngine")
        if preference == "gpu":
            # An explicit GPU engine request never falls back to another GPU
            # backend. GPU_TRIANGLE is admitted only when its evidence row
            # matches; otherwise the step resolves to "unavailable".
            candidates = [requested_gpu_engine or "GPU_DISK"]
        elif preference == "auto" and project.get("schemaVersion") == 7:
            # AUTO keeps its historical GPU_DISK/CPU_DISK policy; it never
            # selects GPU_TRIANGLE on its own.
            candidates = ["GPU_DISK", "CPU_DISK"]
        else:
            candidates = ["CPU_DISK"]
    else:
        candidates = ["GPU_DISK", "GPU_TRIANGLE"] if preference == "gpu" else ["CPU_DISK", "CPU_TRIANGLE"]
    boundary = project["domain"].get("boundaryXY", project["domain"].get("xBoundary"))
    signature = gpu_project_signature(project, step)
    experimental_cpu = False
    if step.get("model") == "specialized":
        spec = model_spec(str(step.get("nativeModel", "")))
        experimental_cpu = bool(spec and spec.get("experimental"))
    for engine in candidates:
        # Matrix entries describe measured coverage; an absent row is not an
        # engine capability failure. The supervisor resolves the actual device
        # and the bounded worker reports native errors without CPU fallback.
        if engine == "GPU_DISK":
            return engine
        if experimental_cpu and engine == "CPU_DISK":
            # Real native CPU smoke coverage exists for these models; the only
            # missing piece is an archived Stage 8/9 acceptance row, which
            # validation reports as a warning rather than hiding the model.
            return engine
        limitation = known_engine_limitation(project["dimension"], model, engine, step)
        if project.get("schemaVersion") == 7 and limitation:
            if analysis_mode == "flux":
                strategy_verified = capability_verified(
                    project["dimension"], model, step["operation"], boundary, engine,
                    schema_version=project.get("schemaVersion"),
                    flux_strategy=GPU_MULTIPARTICLE_FLUX_STRATEGY, **signature)
            else:
                strategy_verified = capability_verified(
                    project["dimension"], model, step["operation"], boundary, engine,
                    schema_version=project.get("schemaVersion"),
                    execution_strategy=GPU_MULTIPARTICLE_SPLIT_STRATEGY, **signature)
            if not strategy_verified:
                continue
        if project.get("schemaVersion") == 7 and known_native_failure(
                project["dimension"], model, step["operation"], boundary,
                project.get("geometry", {}).get("kind"), engine):
            continue
        if capability_verified(project["dimension"], model, step["operation"], boundary, engine,
                               schema_version=project.get("schemaVersion"), **signature):
            return engine
    return "unavailable"


def validate_3d(project, *, kind="project"):
    issues=[]; warnings=[]
    def issue(path,message): issues.append(FieldIssue(path,message))
    d=project.domain; g=project.geometry; delta=d.gridDelta
    if kind=="simulate" and not any(s.enabled for s in project.steps):issue("steps","simulate requires at least one enabled process step")
    if project.solver.cpuThreads > available_cpu_threads():
        issue("solver.cpuThreads", "thread count exceeds available CPU threads")
    if g.kind=="plane":
        if abs(g.height)>100:issue("geometry.height","height exceeds the 100 um application budget")
    elif g.kind in ("fin", "gds"):
        if g.kind == "fin":
            if g.width >= d.xExtent: issue("geometry.width", "fin width must fit inside the lateral domain")
            if g.height > 100: issue("geometry.height", "fin height exceeds the 100 um application budget")
            if g.extrusion is not None and abs(g.extrusion.extent[1] - g.extrusion.extent[0]) > 40:
                issue("geometry.extrusion.extent", "fin extrusion exceeds the 40 um application budget")
        else:
            for axis, value in enumerate(g.position):
                if abs(value) > 1_000_000: issue(f"geometry.position[{axis}]", "GDS placement exceeds the application coordinate budget")
    else:
        width=g.width if g.kind=="trench" else 2*g.radius
        bottom=width-2*g.depth*math.tan(math.radians(g.taperAngle))
        spread=2*g.maskHeight*abs(math.tan(math.radians(g.maskTaperAngle)))
        if bottom<2*delta:issue("geometry.taperAngle","bottom opening must span at least two grid intervals")
        extent=d.xExtent if g.kind=="trench" else min(d.xExtent,d.yExtent)
        if max(width,bottom,width+spread)>=extent:issue("geometry.width" if g.kind=="trench" else "geometry.radius","opening, bottom and mask must fit inside both applicable lateral bounds")
        if g.maskHeight>0 and width-spread<2*delta:issue("geometry.maskTaperAngle","mask opening must span two grid intervals")
        for path,size in [("width" if g.kind=="trench" else "radius",g.width if g.kind=="trench" else g.radius),("depth",g.depth),("maskHeight",g.maskHeight)]:
            if path=="maskHeight" and size==0:continue
            if size<2*delta:issue("geometry."+path,"positive features must span at least two grid intervals")
            elif size<5*delta:warnings.append("geometry."+path+" spans fewer than five grid intervals")
            if size>20:issue("geometry."+path,"feature exceeds 20 um application budget")
    payload=project.model_dump(mode="json",exclude_none=True)
    for index,s in enumerate(project.steps):
        if not s.enabled or s.rateMagnitude==0:continue
        engine=select_engine(payload,payload["steps"][index]) if s.model=="singleParticle" else "not-applicable"
        if not engine.startswith("GPU_") and not capability_verified(3,s.model,s.operation,d.boundaryXY,engine,schema_version=project.schemaVersion):issue("solver.enginePreference","unverified 3D model/operation/boundary/engine combination")
    if issues:raise ProjectInputError(issues)
    return project,warnings
