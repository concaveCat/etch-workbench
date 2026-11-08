"""Shared guards for the ViennaPS 4.7.0 implant / anneal / analysis capability.

The native classes (``viennaps.IonImplantation``, ``Anneal``, ``NetDoping``,
``SheetResistance``) only exist in the 4.7.0 runtime and all of them operate on
the *cell set* stored on the Domain, not on the level-set surface. This module
holds the small, dependency-free rules that the API validator, the artifact
preflight and the worker must agree on:

* which step models belong to the cell-set capability;
* the fixed workbench length unit (um) expressed in centimetres, which the
  native implant / sheet-resistance models need for their cm-based physics;
* label provenance (which step produces which cell-set scalar label);
* the shared fixed-grid rule for every cell-set step (a surface-moving step in
  between is allowed: the worker regenerates and remaps the cell set);
* numeric guard rails that a hand-edited worker input can never bypass.

Nothing here imports ViennaPS, so it is safe in the API process.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable

CELL_SET_STEP_MODELS = frozenset({"implant", "anneal", "analysis"})

#: Schema-3+ projects declare ``units.length = "um"``. The native implant and
#: sheet-resistance models take their length unit in centimetres.
LENGTH_UNIT_CM = 1e-4
LENGTH_UNIT_NAME = "um"

#: DenseCellSet field names the installed runtime always writes.
NATIVE_CELL_SET_LABELS = ("Material", "FillingFraction")

#: A user-supplied step label may never shadow a native system scalar. The
#: native implant writes its concentration into the scalar with the requested
#: name; ``Material`` therefore overwrote the per-cell material ID (measured:
#: 1500 cells got IDs around 5.6e8) and every later step read a corrupt cell set.
RESERVED_CELL_SET_LABELS = frozenset(NATIVE_CELL_SET_LABELS)

#: Upper bound for one step's native cell set. The generic cell-set output uses
#: the same hard construction limit; an implant grid above it is refused before
#: the native grid is allocated.
MAX_STEP_CELL_SET_CELLS = 2_000_000

LABEL_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
SPECIES_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")


def step_value(step: Any, key: str, default: Any = None) -> Any:
    return step.get(key, default) if isinstance(step, dict) else getattr(step, key, default)


def is_cell_set_step(step: Any) -> bool:
    return step_value(step, "model") in CELL_SET_STEP_MODELS


def step_cell_set(step: Any) -> dict[str, Any] | None:
    settings = step_value(step, "cellSet")
    return settings if isinstance(settings, dict) else None


def valid_label(name: Any) -> bool:
    return isinstance(name, str) and bool(LABEL_PATTERN.fullmatch(name))


def reserved_label_error(name: Any, field: str) -> str | None:
    """Return why ``name`` may not be used as a step-owned cell-set label.

    ``Material`` and ``FillingFraction`` are written by the native cell set
    itself. A concentration/damage/active/net-doping/sheet label with one of
    those names makes the native model overwrite the system scalar: the
    measured ``labels.concentration="Material"`` case replaced the material ID
    of 1500 cells with concentration values (~5.6e8).
    """
    if isinstance(name, str) and name in RESERVED_CELL_SET_LABELS:
        return (f"{field} {name!r} is reserved by the native DenseCellSet "
                f"(system labels: {', '.join(NATIVE_CELL_SET_LABELS)}) and cannot be used as a "
                "step-owned cell-set label; the native model would overwrite the system scalar")
    return None


def produced_labels(step: Any) -> set[str]:
    """Return the cell-set scalar labels a step writes when it runs.

    Only labels proven by the installed 4.7.0 bindings are listed. A damage
    label is written only when a damage model is configured, and an active
    label only by a solid-activation anneal.
    """
    model = step_value(step, "model")
    labels: set[str] = set()
    if model == "implant":
        resolved = step_value(step, "labels")
        if isinstance(resolved, dict):
            if resolved.get("concentration"):
                labels.add(str(resolved["concentration"]))
            if step_value(step, "damage") is not None:
                if resolved.get("damage"):
                    labels.add(str(resolved["damage"]))
                if resolved.get("lastDamage"):
                    labels.add(str(resolved["lastDamage"]))
    elif model == "anneal":
        active = step_value(step, "activeLabel")
        if isinstance(active, str) and active:
            labels.add(active)
    elif model == "analysis":
        net = step_value(step, "netDoping")
        if isinstance(net, dict) and net.get("outputLabel"):
            labels.add(str(net["outputLabel"]))
    return labels


def consumed_labels(step: Any) -> set[str]:
    """Return the cell-set scalar labels a step reads."""
    model = step_value(step, "model")
    labels: set[str] = set()
    if model == "anneal":
        species = step_value(step, "speciesLabel")
        if isinstance(species, str) and species:
            labels.add(species)
    elif model == "analysis":
        net = step_value(step, "netDoping")
        if isinstance(net, dict):
            labels.update(str(name) for name in (net.get("donorLabels") or []))
            labels.update(str(name) for name in (net.get("acceptorLabels") or []))
        for row in step_value(step, "sheetResistance") or []:
            name = row.get("concentrationLabel") if isinstance(row, dict) else None
            if isinstance(name, str) and name:
                labels.add(name)
    return labels


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def numeric_guard_messages(step: Any, *, dimension: int, grid_delta: float) -> list[str]:
    """Guard rails shared by the API validator and the worker.

    The Pydantic models already reject non-finite numbers and out-of-range
    values; this function repeats the checks a hand-edited worker input could
    otherwise skip before reaching a native constructor.
    """
    messages: list[str] = []
    model = step_value(step, "model")
    if model not in CELL_SET_STEP_MODELS:
        return messages
    settings = step_cell_set(step)
    if not isinstance(settings, dict):
        messages.append("cellSet block is required")
        return messages
    height = settings.get("height")
    if not _finite(height) or float(height) <= 0.0:
        messages.append("cellSet.height must be a finite positive number")
    elif float(height) + 1e-12 < float(grid_delta):
        messages.append(
            f"cellSet.height {float(height):g} um is below one grid interval ({float(grid_delta):g} um); "
            "the installed DenseCellSet generator would crash")
    if dimension not in (2, 3):
        messages.append("cell-set steps require a 2D or 3D project")
    cover = str(settings.get("coverMaterial") or "GAS")
    if cover != "GAS":
        # Measured on 4.7.0: ``generateCellSet(height, Si, True)`` on a Si
        # substrate makes every cell a cover cell, so the substrate is skipped
        # and the implant writes an all-zero field (P_total max = 0 in 2D/3D).
        # No non-GAS cover has been proven to keep the target visible, so the
        # field steps require GAS instead of silently voiding the substrate.
        messages.append(
            f"cellSet.coverMaterial {cover!r} is not supported for implant/anneal/analysis: the native "
            "DenseCellSet generator treats the cover material as the phase above the surface and skips "
            "cells of that material, so a cover equal to the implanted substrate voids the target and "
            "returns an all-zero field (measured: Si cover on a Si substrate). Use \"GAS\"")
    if settings.get("periodic") and dimension == 3:
        # The native periodic cell-set boundary is flag-per-axis and the 3D
        # Domain boundary is applied to x and y; z stays non-periodic.
        pass
    if model == "implant":
        messages.extend(_implant_guard_messages(step))
    elif model == "anneal":
        messages.extend(_anneal_guard_messages(step))
    elif model == "analysis":
        messages.extend(_analysis_guard_messages(step))
    return messages


def _implant_guard_messages(step: Any) -> list[str]:
    messages: list[str] = []
    profile = step_value(step, "profile")
    if not isinstance(profile, dict):
        messages.append("profile block is required")
        return messages
    if profile.get("source") != "manual":
        messages.append("only modelSource=manual is supported")
        return messages
    kind = profile.get("kind")
    if kind not in {"pearsonIV", "dualPearsonIV", "pearsonIVChanneling"}:
        messages.append(f"unsupported manual profile kind {kind!r}")
        return messages
    for name, side in (("head", profile.get("head")), ("tail", profile.get("tail"))):
        if side is None:
            if name == "head" or kind == "dualPearsonIV":
                messages.append(f"profile.{name} moments are required for {kind}")
            continue
        if not isinstance(side, dict):
            messages.append(f"profile.{name} must be an object")
            continue
        sigma = side.get("sigma")
        if not _finite(sigma) or float(sigma) <= 0.0:
            messages.append(f"profile.{name}.sigma must be a finite positive number")
    for key in ("lateralSigma", "headLateralSigma", "tailLateralSigma"):
        value = profile.get(key)
        if value is not None and (not _finite(value) or float(value) <= 0.0):
            messages.append(f"profile.{key} must be a finite positive number")
    if kind == "dualPearsonIV":
        fraction = profile.get("headFraction")
        if not _finite(fraction) or not 0.0 <= float(fraction) <= 1.0:
            messages.append("profile.headFraction must be within [0, 1]")
    if kind == "pearsonIVChanneling":
        for key, minimum in (("tailFraction", 0.0), ("tailStartDepth", 0.0),
                             ("tailDecayLength", 0.0), ("tailBlendWidth", 0.0)):
            value = profile.get(key)
            if value is None:
                messages.append(f"profile.{key} is required for pearsonIVChanneling")
            elif not _finite(value) or float(value) < minimum:
                messages.append(f"profile.{key} must be a finite non-negative number")
    dose = step_value(step, "doseCm2")
    if not _finite(dose) or float(dose) <= 0.0:
        messages.append("doseCm2 must be a finite positive number")
    tilt = step_value(step, "tiltDeg")
    if not _finite(tilt) or not 0.0 <= float(tilt) <= 90.0:
        messages.append("tiltDeg must be within [0, 90]")
    labels = step_value(step, "labels")
    if isinstance(labels, dict):
        for key in ("concentration", "damage", "lastDamage"):
            error = reserved_label_error(labels.get(key), f"labels.{key}")
            if error is not None:
                messages.append(error)
    return messages


def _anneal_guard_messages(step: Any) -> list[str]:
    messages: list[str] = []
    schedule = step_value(step, "schedule")
    if not isinstance(schedule, dict):
        messages.append("schedule block is required")
        return messages
    kind = schedule.get("kind")
    if kind == "isothermal":
        temperature = schedule.get("temperatureK")
        duration = schedule.get("durationSeconds")
        if not _finite(temperature) or float(temperature) <= 0.0:
            messages.append("schedule.temperatureK must be positive")
        if not _finite(duration) or float(duration) < 0.0:
            messages.append("schedule.durationSeconds must be non-negative")
    elif kind == "schedule":
        durations = schedule.get("durations") or []
        temperatures = schedule.get("temperatures") or []
        if len(temperatures) not in (len(durations), len(durations) + 1) or not durations:
            messages.append("schedule temperatures must contain N (isothermal) or N+1 (ramp) values")
        for value in durations:
            if not _finite(value) or float(value) < 0.0:
                messages.append("schedule.durations must be finite and non-negative")
                break
        for value in temperatures:
            if not _finite(value) or float(value) <= 0.0:
                messages.append("schedule.temperatures must be finite and positive")
                break
    else:
        messages.append(f"unsupported schedule kind {kind!r}")
    diffusivity = step_value(step, "diffusivity")
    if not isinstance(diffusivity, dict):
        messages.append("diffusivity block is required")
    elif diffusivity.get("kind") == "arrhenius":
        if not _finite(diffusivity.get("D0")) or float(diffusivity["D0"]) <= 0.0:
            messages.append("diffusivity.D0 must be positive")
        if not _finite(diffusivity.get("Ea_eV")) or float(diffusivity["Ea_eV"]) < 0.0:
            messages.append("diffusivity.Ea_eV must be non-negative")
    elif diffusivity.get("kind") == "constant":
        if not _finite(diffusivity.get("value")) or float(diffusivity["value"]) < 0.0:
            messages.append("diffusivity.value must be non-negative")
    else:
        messages.append("diffusivity.kind must be arrhenius or constant")
    activation = step_value(step, "solidActivation")
    if not isinstance(activation, dict):
        messages.append("solidActivation block is required")
    else:
        if not _finite(activation.get("solubilityC0")) or float(activation["solubilityC0"]) <= 0.0:
            messages.append("solidActivation.solubilityC0 must be positive")
        if not _finite(activation.get("solubilityEa_eV")) or float(activation["solubilityEa_eV"]) < 0.0:
            messages.append("solidActivation.solubilityEa_eV must be non-negative")
    for key in ("speciesLabel", "activeLabel"):
        error = reserved_label_error(step_value(step, key), key)
        if error is not None:
            messages.append(error)
    return messages


def _analysis_guard_messages(step: Any) -> list[str]:
    messages: list[str] = []
    net = step_value(step, "netDoping")
    sheet = step_value(step, "sheetResistance") or []
    if net is None and not sheet:
        messages.append("analysis requires netDoping or at least one sheetResistance entry")
    if isinstance(net, dict):
        output = net.get("outputLabel")
        if not valid_label(output):
            messages.append("netDoping.outputLabel must be a valid cell-set label")
        else:
            error = reserved_label_error(output, "netDoping.outputLabel")
            if error is not None:
                messages.append(error)
        for key in ("donorLabels", "acceptorLabels"):
            for index, name in enumerate(net.get(key) or []):
                error = reserved_label_error(name, f"netDoping.{key}[{index}]")
                if error is not None:
                    messages.append(error)
        if not (net.get("donorLabels") or net.get("acceptorLabels")):
            messages.append("netDoping requires at least one donor or acceptor label")
    for index, row in enumerate(sheet):
        if not isinstance(row, dict):
            messages.append(f"sheetResistance[{index}] must be an object")
            continue
        if not valid_label(row.get("concentrationLabel")):
            messages.append(f"sheetResistance[{index}].concentrationLabel must be a valid cell-set label")
        else:
            error = reserved_label_error(row.get("concentrationLabel"),
                                         f"sheetResistance[{index}].concentrationLabel")
            if error is not None:
                messages.append(error)
        if row.get("carrier") not in {"electron", "hole"}:
            messages.append(f"sheetResistance[{index}].carrier must be electron or hole")
    return messages


def field_grid_signature(step: Any) -> dict[str, Any]:
    """Return the settings that define the one shared native cell grid."""
    settings = step_cell_set(step) or {}
    substrate = settings.get("substrateDepth")
    return {
        "height": float(settings.get("height") or 0.0),
        "substrateDepth": None if substrate is None else float(substrate),
        "coverMaterial": str(settings.get("coverMaterial") or "GAS"),
        "periodic": bool(settings.get("periodic", False)),
    }


def initial_geometry_vertical_bounds(geometry: Any,
                                     dimension: int) -> tuple[float, float] | None:
    """Return the initial surface ``(top, bottom)`` for a parametric geometry.

    ``Domain.generateCellSet(position, ...)`` takes the *absolute* top of the
    grid, so a field ``cellSet.height`` below the initial surface top would
    either clip the surface or (measured on 4.7.0) make the native generator
    walk the whole surface band down to the requested position and allocate a
    huge grid. The API can reject such a request statically for every geometry
    whose vertical extent is fully described by the project.

    ``None`` means the vertical extent is not knowable from the project alone
    (GDS layout or a saved Domain continuation); the worker still performs the
    same check against the real surface before allocating.
    """
    if not isinstance(geometry, dict):
        geometry = geometry.model_dump(mode="json", exclude_none=True)
    kind = geometry.get("kind")
    if kind == "plane":
        height = float(geometry.get("height", 0.0))
        return height, height
    if kind in {"trench", "hole"}:
        # MakeTrench/MakeHole: substrate at z=0, opening down to -depth, mask
        # above z=0 up to maskHeight.
        return float(geometry.get("maskHeight", 0.0)), -float(geometry.get("depth", 0.0))
    if kind == "fin":
        # MakeFin: the fin sits on z=0, the mask on its top.
        return (float(geometry.get("height", 0.0)) + float(geometry.get("maskHeight", 0.0)),
                0.0)
    if kind == "stack":
        base = float(geometry.get("baseHeight", 0.0))
        top = base + sum(abs(float(row.get("thickness", 0.0)))
                         for row in geometry.get("layers") or [])
        bottom = base
        mask = geometry.get("gdsMask")
        if isinstance(mask, dict):
            position = mask.get("position") or [0.0, 0.0, 0.0]
            mask_bottom = float(mask.get("baseHeight", 0.0)) + float(position[2])
            top = max(top, mask_bottom + float(mask.get("height", 0.0)))
            bottom = min(bottom, mask_bottom)
        return top, bottom
    return None


def field_geometry_issues(steps: Iterable[Any], *, geometry: Any, dimension: int,
                          grid_delta: float, x_extent: float, y_extent: float,
                          from_domain_state: bool = False) -> list[tuple[int, str, str]]:
    """Return static pre-allocation issues for the first cell-set grid.

    * ``cellSet.height`` is the native **absolute** grid top. A value below the
      highest initial surface point is rejected instead of letting the native
      generator build a band from that position up to the surface.
    * The cell count implied by the surface top/bottom, ``substrateDepth`` and
      the domain's lateral grid is checked against ``MAX_CELL_SET_CELLS`` before
      the worker allocates the dense grid.

    The checks only apply when the field step is the first enabled operation:
    an earlier enabled step (planarize, boolean, process, ...) can move the
    surface arbitrarily, so the original geometry no longer bounds it and the
    worker's live pre-allocation check decides. GDS geometry and Domain
    continuations likewise have no project-level vertical extent.
    """
    from .runtime_limits import MAX_CELL_SET_CELLS

    bounds = None if from_domain_state else initial_geometry_vertical_bounds(
        geometry, dimension)
    if bounds is None:
        return []
    first: tuple[int, Any] | None = None
    for index, step in enumerate(steps):
        if step_value(step, "enabled") and step_value(step, "model") in CELL_SET_STEP_MODELS:
            first = (index, step)
            break
    if first is None:
        return []
    index, step = first
    if any(step_value(earlier, "enabled") for earlier in steps[:index]):
        # A preceding enabled operation (planarize, boolean, a process step,
        # ...) can move the surface arbitrarily before the grid is built, so the
        # original geometry bounds no longer describe it. The worker checks the
        # live surface before it allocates; a static check here would reject
        # valid sequences such as plane(2) -> planarize(0) -> implant.
        return []
    settings = step_cell_set(step) or {}
    if settings.get("height") is None:
        return []
    delta = float(grid_delta)
    height = float(settings["height"])
    surface_top, surface_bottom = float(bounds[0]), float(bounds[1])
    top_node = math.ceil(surface_top / delta - 1e-9) * delta
    issues: list[tuple[int, str, str]] = []
    if height + 1e-9 < top_node:
        issues.append((
            index, "cellSet.height",
            f"cellSet.height {height:g} um is below the highest initial surface point "
            f"{surface_top:g} um; the native generateCellSet position is an absolute coordinate, "
            "so the field grid would clip the surface (and, below the surface band, the native "
            "generator allocates the whole band between them). Set cellSet.height above the "
            "surface or lower the geometry"))
    substrate = settings.get("substrateDepth")
    if substrate is None:
        return issues
    substrate = float(substrate)
    nx = max(1, math.ceil(float(x_extent) / delta))
    ny = (max(1, math.ceil(float(y_extent) / delta)) if int(dimension) == 3 else 1)
    lateral = (nx + 2) * (ny + 2)
    bottom_node = math.floor(surface_bottom / delta + 1e-9) * delta
    band_half = math.ceil(substrate / delta) + 1
    required_top = max(height, top_node)
    required_bottom = bottom_node - band_half * delta
    rows = math.ceil((required_top - required_bottom) / delta) + 4
    estimate = lateral * max(rows, 2)
    if estimate > MAX_CELL_SET_CELLS:
        issues.append((
            index, "cellSet",
            f"the field grid would need about {estimate:,} cells between the highest surface "
            f"point ({surface_top:g} um) and substrateDepth below the deepest point "
            f"({surface_bottom:g} um), above the {MAX_CELL_SET_CELLS:,}-cell construction limit; "
            "raise cellSet.height above the surface only as needed, reduce substrateDepth, or "
            "coarsen gridDelta"))
    return issues



def field_sequence_issues(steps: Iterable[Any], *, grid_delta: float,
                          domain_periodic: bool | None = None) -> list[tuple[int, str, str]]:
    """Return ``(step_index, field_path, message)`` for grid-sequence violations.

    The native ``DenseCellSet`` grid is captured at the first enabled field
    step, and every later implant/anneal/analysis step uses the same grid
    definition. This function is the single shared implementation for the API
    validator and the worker preflight, so a hand-edited ``input.json`` cannot
    silently change the grid: it rejects

    * a later field step asking for a different ``height``/``substrateDepth``/
      ``coverMaterial``/``periodic``;
    * the first enabled field step omitting ``substrateDepth``;
    * a grid-defining step below one grid interval / a substrate depth below
      two grid intervals.

    ``field_path`` is ``""`` for a whole-step issue, otherwise a dotted path
    below the step (``cellSet.height``). A surface-moving step between two field
    steps is *not* an issue: the worker regenerates the native cell set and
    conservatively remaps every scalar field (see
    ``backend/field_remap.py``).
    """
    issues: list[tuple[int, str, str]] = []
    first: dict[str, Any] | None = None
    for index, step in enumerate(steps):
        if not step_value(step, "enabled"):
            continue
        model = step_value(step, "model")
        if model not in CELL_SET_STEP_MODELS:
            continue
        signature = field_grid_signature(step)
        height = signature["height"]
        if height + 1e-12 < float(grid_delta):
            issues.append((index, "cellSet.height",
                           f"cellSet.height {height:g} um is below one grid interval ({float(grid_delta):g} um); "
                           "the installed DenseCellSet generator would crash"))
        substrate = signature["substrateDepth"]
        if substrate is not None and substrate + 1e-12 < 2.0 * float(grid_delta):
            issues.append((index, "cellSet.substrateDepth",
                           f"cellSet.substrateDepth must span at least two grid intervals ({2.0 * float(grid_delta):g} um)"))
        if domain_periodic is not None and signature["periodic"] != bool(domain_periodic):
            issues.append((index, "cellSet.periodic",
                           "cellSet.periodic must match the domain lateral boundary; the cell set is "
                           "generated from the domain's level sets and only the native neighbor topology "
                           "can be marked periodic"))
        if first is None:
            first = signature
            if substrate is None:
                issues.append((index, "cellSet.substrateDepth",
                               "the first enabled cell-set field step must declare substrateDepth: a native "
                               "ViennaLS level set stores only a narrow band, so without it the DenseCellSet "
                               "would contain a single substrate row and the dopant profile would have no depth"))
        else:
            for key in ("height", "coverMaterial", "periodic"):
                if signature[key] != first[key]:
                    issues.append((index, f"cellSet.{key}",
                                   "every cell-set step shares the grid generated by the first field step; "
                                   f"{key} must match that step ({first[key]!r})"))
            if substrate is not None and first["substrateDepth"] is not None \
                    and abs(substrate - float(first["substrateDepth"])) > 1e-12:
                issues.append((index, "cellSet.substrateDepth",
                               "every cell-set step shares the grid generated by the first field step; "
                               "substrateDepth must match that step or be omitted"))
    return issues


def label_provenance_issues(steps: Iterable[Any]) -> list[tuple[int, str]]:
    """Check every consumed cell-set label is produced earlier in the run."""
    issues: list[tuple[int, str]] = []
    available: set[str] = set(NATIVE_CELL_SET_LABELS)
    seen_field_step = False
    for index, step in enumerate(steps):
        if not step_value(step, "enabled"):
            continue
        model = step_value(step, "model")
        if model in CELL_SET_STEP_MODELS:
            if model == "analysis" and not seen_field_step:
                issues.append((index, "analysis requires an earlier implant or anneal step that creates the dopant field"))
            missing = sorted(consumed_labels(step) - available)
            if missing:
                issues.append((index, "cell-set label(s) not produced by an earlier step: " + ", ".join(missing)))
            available |= produced_labels(step)
            seen_field_step = True
    return issues


def cell_set_output_conflict(project: Any) -> str | None:
    """Reject the generic cell-set output next to a cell-set field step.

    ``outputSettings.cellSet`` builds a *fresh* grid from the level sets, so it
    cannot contain the implant fields. Offering both would silently produce a
    field-less cell set next to the real one; the step's own output is the only
    source of truth.
    """
    steps = project.get("steps") if isinstance(project, dict) else getattr(project, "steps", [])
    if not any(step_value(step, "enabled") and is_cell_set_step(step) for step in steps):
        return None
    settings = project.get("outputSettings") if isinstance(project, dict) else getattr(project, "outputSettings", None)
    if settings is None:
        return None
    cell_set = settings.get("cellSet") if isinstance(settings, dict) else getattr(settings, "cellSet", None)
    enabled = cell_set.get("enabled") if isinstance(cell_set, dict) else getattr(cell_set, "enabled", False)
    if enabled:
        return ("outputSettings.cellSet is not available together with implant/anneal/analysis steps; "
                "each cell-set step writes its own native DenseCellSet with the dopant fields")
    return None
