"""Installed ViennaPS 4.6.2 specialized process allowlist and UI metadata."""
from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from .engine import OXIDATION_ADVANCED_RESTRICTED, engine_version, faraday_2d_error


def number(key: str, default: float, *, minimum: float = 0.0, maximum: float = 1_000_000.0,
           step: float = 0.1, label: str | None = None, unit: str | None = None) -> dict[str, Any]:
    return {"key": key, "label": label or key, "kind": "number", "default": default,
            "minimum": minimum, "maximum": maximum, "step": step, "nativeUnit": unit}


def enum(key: str, default: str, values: list[tuple[str, str]], *, label: str | None = None) -> dict[str, Any]:
    return {"key": key, "label": label or key, "kind": "enum", "default": default,
            "options": [{"value": value, "label": title} for value, title in values]}


def rates(key: str, default: float = 0.0, *, label: str = "材料速率表") -> dict[str, Any]:
    return {"key": key, "label": label, "kind": "materialRates", "default": default,
            "minimum": 0.0, "maximum": 1_000_000.0, "step": 0.01, "nativeUnit": None}


# ``SelectiveEpitaxy`` binds a third constructor overload that takes only a
# fixed-size three-vector of normal-vector factors (plus rate111/rate100). The
# installed header computes ``max_i(|nvFactors[i] * normal[i]|)`` and maps that
# scalar from [1/sqrt(D), 1] to [rate111, rate100]. Values outside [0, 1] push
# the interpolation outside that native range, so only the documented range is
# exposed. The native defaults are dimension-dependent and are NOT [1, 1, 1]:
# 2D -> [0.5, 1, 0], 3D -> [0.5, 0, 1]. Omitting the key keeps the existing
# materialRates overload and therefore the exact current request hash/artifacts.
NV_FACTORS_FIELD: dict[str, Any] = {
    "key": "nvFactors",
    "label": "法向方向因子 [x, y, z]",
    "kind": "nvFactors",
    "length": 3,
    "default": None,
    "minimum": 0.0,
    "maximum": 1.0,
    "step": 0.01,
    "nativeUnit": None,
    "nativeClass": "SelectiveEpitaxy",
    "nativeConstructor": "viennaps.d{D}.SelectiveEpitaxy(nvFactors, rate111, rate100)",
    "nativeDefaultByDimension": {"2": [0.5, 1.0, 0.0], "3": [0.5, 0.0, 1.0]},
    "defaultNote": ("omit to use the installed materialRates constructor and its native dimension default; "
                    "an explicit vector uses the separate nvFactors overload and applies materialRates through setMaterialRate"),
}


def coverage_tolerance_field() -> dict[str, Any]:
    """Optional native ``CoverageParameters.tolerance`` override.

    The worker has always pinned 1e-3 for the four plasma models while the
    native struct default is 0.0. Omitting the key keeps that historical
    value; an explicit value is passed to the same native object. Negative,
    non-finite or >1 values are rejected because the native convergence test
    is ``delta > tolerance``: a negative/NaN value never converges and the
    installed strategy then iterates up to the unsigned default
    (4294967295) instead of the 10-iteration fallback used for tolerance 0.
    """
    return {
        "key": "coverageTolerance",
        "label": "覆盖率收敛容差",
        "kind": "number",
        "default": 0.001,
        "minimum": 0.0,
        "maximum": 1.0,
        "step": 0.0001,
        "nativeUnit": None,
        "nativeClass": "CoverageParameters",
        "nativeMember": "tolerance",
        "nativeDefault": 0.0,
        "workbenchHistoricalDefault": 0.001,
        "defaultSource": "workbench",
        "defaultNote": ("omit to keep the Worker's historical 1e-3; the native default 0.0 leaves maxIterations "
                        "at its default and is capped to 10 coverage-initialization iterations"),
    }


FLUOROCARBON_MATERIAL_DEFAULTS = {
    "A_ie": 0.0361, "A_sp": 0.0139, "B_sp": 9.3, "E_a": 0.168,
    "Eth_ie": 4.0, "Eth_sp": 18.0, "K": 0.002789491704544977,
    "beta_e": 0.9, "beta_p": 0.26, "density": 2.2,
}


PLASMA_COVERAGE_MODELS = frozenset({"sf6o2", "cf4o2", "hbro2", "sf6c4f8"})
GEOMETRIC_TRENCH_ARGUMENT_KEYS = ("trenchWidth", "trenchDepth", "depositionRate",
                                  "bottomMed", "a", "b", "n")
GEOMETRIC_TRENCH_GEOMETRIES = frozenset({"plane", "trench", "hole", "fin", "stack"})


def _object_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def finite_number(value: Any) -> float | None:
    """Return a finite float or None, including for oversized Python ints."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def nv_factors_issues(value: Any) -> list[str]:
    """Validate the three native normal-vector factors."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return ["must contain exactly three finite numbers"]
    numbers: list[float] = []
    for item in value:
        number = finite_number(item)
        if number is None:
            return ["must contain exactly three finite numbers"]
        numbers.append(number)
    if any(number < 0.0 or number > 1.0 for number in numbers):
        return ["each factor must be between 0.0 and 1.0; the native interpolation maps "
                "max_i(|factor_i*normal_i|) from [1/sqrt(D), 1] to [rate111, rate100]"]
    if not any(number > 0.0 for number in numbers):
        return ["must contain at least one positive factor; an all-zero vector drives the native "
                "velocity to zero and leaves the surface unchanged"]
    return []


def coverage_tolerance_issues(value: Any) -> list[str]:
    """Validate the optional native CoverageParameters.tolerance."""
    number = finite_number(value)
    if number is None:
        return ["must be a finite JSON number"]
    if not 0.0 <= number <= 1.0:
        return ["must be between 0.0 and 1.0; a negative or non-finite tolerance never converges "
                "and the native strategy would iterate up to its unsigned maxIterations default"]
    return []


def geometric_trench_vertical_extent(geometry: dict[str, Any]) -> float:
    """Conservative max |z| of the initial geometry seen by the native radius.

    MakeFin places the mask above the fin, so the fin's vertical span is
    ``finHeight + maskHeight``. MakeTrench/MakeHole put the mask above z=0 and
    the opening below it, so their span is ``max(depth, maskHeight)``. Taper
    angles change lateral width only; the installed constructors do not add
    vertical extent for a taper.
    """
    kind = geometry.get("kind")
    if kind == "plane":
        return abs(float(geometry.get("height", 0.0)))
    if kind in {"trench", "hole"}:
        return max(abs(float(geometry.get("depth", 0.0))),
                   abs(float(geometry.get("maskHeight", 0.0))))
    if kind == "fin":
        return (abs(float(geometry.get("height", 0.0)))
                + abs(float(geometry.get("maskHeight", 0.0))))
    if kind == "stack":
        base = float(geometry.get("baseHeight", 0.0))
        top = base + sum(abs(float(row.get("thickness", 0.0)))
                         for row in geometry.get("layers", []))
        bottom = base
        # A legacy/overlay GDS mask is a separate finite body above the stack.
        # Its absolute z is baseHeight + position[2], independent of the stack
        # baseHeight; ignore it and the native radius formula can be evaluated
        # beyond its valid range (SIGSEGV / emptied level sets).
        mask = geometry.get("gdsMask")
        if isinstance(mask, dict):
            position = mask.get("position") or [0.0, 0.0, 0.0]
            mask_bottom = float(mask.get("baseHeight", 0.0)) + float(position[2])
            mask_top = mask_bottom + float(mask.get("height", 0.0))
            bottom = min(bottom, mask_bottom)
            top = max(top, mask_top)
        return max(abs(bottom), abs(top))
    return 0.0


def geometric_trench_issues(project: Any, step: Any) -> list[tuple[str, str]]:
    """Central safety rule for GeometricTrenchDeposition.

    Both request validation and the worker call this function, so a direct
    worker input cannot bypass the dimension/geometry/depth/reach checks.

    The installed ViennaLS distribution advertises ``+/-depositionRate`` as its
    reach but computes a local radius ``a*(1-|z|/depth)^n + b`` (or
    ``bottomMed`` at the floor). Probing 4.6.2/5.8.5 shows an empty deposited
    level set once ``rate < radius - 2*gridDelta`` and SIGSEGV further below
    that. The worker therefore requires ``depositionRate >= max(a+b,
    bottomMed)``: a deliberately conservative margin that rejects some inputs
    the native kernel happens to accept, but never leaves an empty layer in the
    tested 2D/3D plane/trench/hole/fin/stack matrix.
    """
    issues: list[tuple[str, str]] = []
    dimension = _object_value(project, "dimension")
    if dimension not in (2, 3):
        issues.append(("nativeModel", "GeometricTrenchDeposition is only available in 2D and 3D"))
    geometry = _object_value(project, "geometry", {})
    if not isinstance(geometry, dict):
        geometry = geometry.model_dump(mode="json", exclude_none=True)
    kind = geometry.get("kind")
    if kind == "gds":
        issues.append(("nativeModel",
                       "GeometricTrenchDeposition is not supported with GDS geometry in 2D or 3D; its native "
                       "radius formula needs a bounded vertical span that the project description does not provide"))
    elif kind not in GEOMETRIC_TRENCH_GEOMETRIES:
        issues.append(("nativeModel",
                       f"GeometricTrenchDeposition requires a parametric plane/trench/hole/fin/stack geometry; "
                       f"got {kind!r}"))
    if _object_value(project, "continuationState", None) is not None:
        issues.append(("nativeModel",
                       "GeometricTrenchDeposition is not supported with a continuation state; the saved Domain's "
                       "vertical span cannot be validated from the project description"))
    steps = _object_value(project, "steps", []) or []
    step_id = _object_value(step, "id")
    index = next((position for position, candidate in enumerate(steps)
                  if _object_value(candidate, "id") == step_id), None)
    if index is not None and any(_object_value(previous, "enabled", False)
                                 for previous in steps[:index]):
        issues.append(("nativeModel",
                       "GeometricTrenchDeposition must be the first enabled step; its native radius formula is "
                       "evaluated from absolute z and the validated vertical span cannot account for earlier operations"))
    params = _object_value(step, "nativeParameters", {})
    if not isinstance(params, dict):
        issues.append(("nativeParameters", "must be a JSON object"))
        return issues
    values: dict[str, float] = {}
    for key in GEOMETRIC_TRENCH_ARGUMENT_KEYS:
        number = finite_number(params.get(key))
        if number is None:
            issues.append((f"nativeParameters.{key}", "must be a finite JSON number"))
        else:
            values[key] = number
    if len(values) != len(GEOMETRIC_TRENCH_ARGUMENT_KEYS):
        return issues
    width = values["trenchWidth"]
    depth = values["trenchDepth"]
    rate = values["depositionRate"]
    bottom = values["bottomMed"]
    a_value = values["a"]
    b_value = values["b"]
    n_value = values["n"]
    grid_delta = float(_object_value(_object_value(project, "domain", {}), "gridDelta", 0.0))
    for key in ("trenchWidth", "trenchDepth", "depositionRate"):
        if 0.0 < values[key] < 1e-6:
            issues.append((f"nativeParameters.{key}", "must be at least 0.000001"))
    for key in ("trenchWidth", "trenchDepth", "depositionRate", "bottomMed", "a", "b"):
        if values[key] > 1_000_000.0:
            issues.append((f"nativeParameters.{key}", "must be at most 1000000"))
    if width <= 0.0:
        issues.append(("nativeParameters.trenchWidth", "must be positive"))
    if depth <= 0.0:
        issues.append(("nativeParameters.trenchDepth", "must be positive"))
    if rate <= 0.0:
        issues.append(("nativeParameters.depositionRate", "must be positive"))
    if bottom < 0.0:
        issues.append(("nativeParameters.bottomMed", "must be non-negative"))
    if a_value < 0.0:
        issues.append(("nativeParameters.a", "must be non-negative"))
    if b_value < 0.0:
        issues.append(("nativeParameters.b", "must be non-negative"))
    if (not n_value.is_integer()) or n_value < 1.0 or n_value > 100.0:
        issues.append(("nativeParameters.n",
                       "must be an integer between 1 and 100; fractional n can produce a non-finite native radius"))
    if 0.0 < width < grid_delta - 1e-12:
        issues.append(("nativeParameters.trenchWidth",
                       f"must be at least one grid interval ({grid_delta:g} um); the native kernel ignores this value, "
                       "so a sub-grid trench only misrepresents the geometry"))
    if 0.0 < depth < grid_delta - 1e-12:
        issues.append(("nativeParameters.trenchDepth",
                       f"must be at least one grid interval ({grid_delta:g} um); the native radius divides by this depth"))
    max_z = geometric_trench_vertical_extent(geometry)
    if depth < max_z - 1e-12:
        issues.append(("nativeParameters.trenchDepth",
                       f"must be at least the initial structure's maximum |z| ({max_z:g} um); "
                       "a shallower depth puts the native radius formula outside its valid range and can overflow"))
    top_radius = a_value + b_value
    if top_radius <= 0.0:
        issues.append(("nativeParameters.a",
                       "a+b cannot be zero; a+b and bottomMed must give the duplicated top level set a positive "
                       "native radius, otherwise the deposition is a silent no-op"))
        return issues
    max_radius = max(top_radius, bottom)
    if rate < max_radius - 1e-12:
        issues.append(("nativeParameters.depositionRate",
                       f"must be at least the native radius ({max_radius:g} um); the installed ViennaLS "
                       "GeometricAdvect window is derived from depositionRate, and a smaller rate leaves the "
                       "deposited level set empty or aborts with SIGSEGV"))
    return issues


def ibe_fields() -> list[dict[str, Any]]:
    """Native IBEParameters fields shared by ionBeam and faradayCage.

    FaradayCageParameters wraps this exact native struct in ``ibeParams`` and
    adds ``cageAngle``; keeping one definition prevents the two allowlist rows
    from drifting apart.
    """
    return [number("meanEnergy", 100), number("sigmaEnergy", 10), number("thresholdEnergy", 20),
            number("thetaRMin", 70, maximum=180), number("thetaRMax", 90, maximum=180),
            number("minAngle", 85, maximum=180), number("inflectAngle", 89, maximum=180),
            number("exponent", 100), number("n_l", 10), number("tiltAngle", 0, minimum=-180, maximum=180),
            number("planeWaferRate", 0.1), number("redepositionRate", 0), number("redepositionThreshold", 0.1),
            number("cos4YieldA1", 0), number("cos4YieldA2", 0), number("cos4YieldA3", 0), number("cos4YieldA4", 0),
            rates("materialPlaneWaferRateByMaterial", 0.0, label="材料平面晶圆速率表")]


IBE_ENERGY_MODELS = frozenset({"ionBeam", "faradayCage"})


def ibe_energy_issues(parameters: Any) -> list[tuple[str, str]]:
    """Shared ``thresholdEnergy < meanEnergy`` rule for both IBE models.

    ViennaPS 4.6.2 normalizes the IBE yield by comparing the sampled energy
    with ``thresholdEnergy``. At ``thresholdEnergy >= meanEnergy`` that native
    comparison changes the sign of the flux, so an ``etch`` step deposits:

    * threshold 200 / mean 100 / sigmaEnergy 500 grew a 2D stack from
      0.8 um to 0.969595 um (also in 3D: 0.8 -> 0.974824 um);
    * threshold == mean aborts the ray trace ("No geometry was passed to
      rayTrace") when sigmaEnergy > 0, and empties the top level set when
      sigmaEnergy == 0;
    * threshold > mean with a small sigmaEnergy can also silently no-op
      (0.8 um unchanged) instead of etching.

    Every probed combination with ``thresholdEnergy < meanEnergy``, including
    sigmaEnergy up to the catalog maximum, etched normally.

    The values must already be JSON numbers. They are deliberately not passed
    through ``float()``: ``ProjectV7.nativeParameters`` is a ``dict[str, Any]``
    and accepts ``"200"``, but the worker later calls ``float(p[key])``, so a
    numeric string would otherwise reach the native constructor and grow the
    surface. Booleans, numeric strings, missing keys, and non-finite values are
    rejected here so the worker can stop before any native object exists. Both
    API validation and ``build_specialized_model`` call this function.
    """
    if not isinstance(parameters, dict):
        return [("nativeParameters", "must be a JSON object")]
    issues: list[tuple[str, str]] = []
    values: dict[str, float] = {}
    for key in ("meanEnergy", "thresholdEnergy"):
        if key not in parameters:
            issues.append((f"nativeParameters.{key}",
                           "is required for the IBE energy-order check"))
            continue
        value = parameters[key]
        if isinstance(value, bool):
            issues.append((f"nativeParameters.{key}",
                           "must be a JSON number; booleans are not accepted"))
            continue
        if not isinstance(value, (int, float)):
            issues.append((f"nativeParameters.{key}",
                           "must be a finite JSON number; numeric strings are not coerced"))
            continue
        if not math.isfinite(float(value)):
            issues.append((f"nativeParameters.{key}", "must be a finite JSON number"))
            continue
        values[key] = float(value)
    if issues:
        return issues
    if values["thresholdEnergy"] < values["meanEnergy"]:
        return []
    return [(
        "nativeParameters.thresholdEnergy",
        f"must be less than meanEnergy ({values['meanEnergy']:g}); ViennaPS {engine_version()} compares the "
        f"sampled energy with thresholdEnergy when it normalizes the IonBeamEtching/FaradayCageEtching yield, "
        f"and thresholdEnergy >= meanEnergy reverses the native flux sign. An etch step then grows the surface "
        f"(threshold 200 with mean 100 and sigmaEnergy 500 grew 0.8 um to 0.969595 um), aborts the ray trace "
        f"when thresholdEnergy equals meanEnergy with sigmaEnergy > 0, or empties the top level set when "
        f"sigmaEnergy is 0. Keep thresholdEnergy below meanEnergy.")]


# 2D FaradayCageEtching is only exposed on the pinned CPU disk flux engine.
# ``worker/probe_faraday_2d.py --matrix safe-2d`` runs every catalog
# cageAngle/tiltAngle pair (13 x 9 = 117) plus the key IBEParameters, geometry,
# grid, duration, ray and thread variants through the real worker model builder
# with a 60 s per-case timeout: every case completes in well under 0.02 s and
# every level set stays populated. The same matrix on AUTO/GPU_DISK hangs
# indefinitely for a large region (e.g. cageAngle 0.2/10/30/45 at tiltAngle
# 0/30), which is the "2D hang" the original audit observed: those probes never
# pinned an engine, so ``Process`` resolved AUTO to the GPU kernel. Pin CPU_DISK
# and reject every other engine instead of guessing a cage/tilt sub-region.
FARADAY_CAGE_2D_ENGINES = frozenset({"CPU_DISK"})
FARADAY_CAGE_2D_ENGINE_NOTE = (
    "The ViennaPS 4.6.2 2D FaradayCageEtching kernel only completes on the CPU_DISK flux engine; "
    "AUTO/GPU_DISK/GPU_TRIANGLE hang indefinitely for a large cageAngle/tiltAngle region "
    "(e.g. cageAngle 0.2/10/30/45 with tiltAngle 0/30) while the CPU_DISK sweep over the whole "
    "catalog range (cageAngle 0-180 deg, tiltAngle -90-180 deg) finishes for every combination")


def faraday_cage_2d_engine_error(flux_engine: str | None) -> str | None:
    """Strict shared 2D FaradayCage gate used by API validation and the worker.

    Returns ``None`` only for an explicitly pinned CPU disk engine on a runtime
    whose 2D kernel actually etches. ``None`` (no engine supplied) is rejected
    as well: a direct worker caller must state the engine it is about to pin
    instead of inheriting AUTO.
    """
    version_message = faraday_2d_error()
    if version_message is not None:
        return version_message
    if flux_engine in FARADAY_CAGE_2D_ENGINES:
        return None
    resolved = "no explicit flux engine" if flux_engine is None else f"the {flux_engine} flux engine"
    return (f"2D FaradayCageEtching requires the pinned CPU_DISK flux engine; this request resolves to "
            f"{resolved}. {FARADAY_CAGE_2D_ENGINE_NOTE}. Set solver.enginePreference to \"cpu\".")


def advanced_number(key: str, default: float, *, minimum: float = 0.0, maximum: float = 1_000_000.0,
                    step: float = 0.01, unit: str | None = None) -> dict[str, Any]:
    return {"key": key, "kind": "number", "default": default, "minimum": minimum,
            "maximum": maximum, "step": step, "nativeUnit": unit}


def advanced_boolean(key: str, default: bool) -> dict[str, Any]:
    return {"key": key, "kind": "boolean", "default": default}


# Flat catalog fields are also the effective values the worker passes when
# ``advanced`` is omitted. The advanced schema below takes every shared default
# from these lists, so a client materialising the catalog cannot change
# behaviour by accident.
PLASMA_FLAT_FIELDS = [
    number("ionFlux", 1), number("etchantFlux", 1), number("oxygenFlux", 0.2),
    number("meanIonEnergy", 100), number("sigmaIonEnergy", 10), number("ionExponent", 100),
    number("oxySputterYield", 3), number("etchStopDepth", -1_000_000, minimum=-1_000_000),
]
SF6C4F8_FLAT_FIELDS = [
    number("ionFlux", 1), number("etchantFlux", 1), number("meanEnergy", 100),
    number("sigmaEnergy", 10), number("ionExponent", 300),
    number("etchStopDepth", -1_000_000, minimum=-1_000_000),
]
CF4O2_FLAT_FIELDS = [
    number("ionFlux", 1), number("etchantFlux", 1), number("oxygenFlux", 0.2),
    number("polymerFlux", 0.2), number("meanIonEnergy", 100), number("sigmaIonEnergy", 10),
    number("ionExponent", 100), number("oxySputterYield", 3), number("polySputterYield", 3),
    number("etchStopDepth", -1_000_000, minimum=-1_000_000),
]


# Payload keys are lower-case aliases; ``native`` is the exact installed member
# path. Defaults and ranges come from introspecting ViennaPS 4.6.2
# (PlasmaEtchingParameters*/CF4O2Parameters*) and the flat overloads, not from
# prose documentation. ``tests/test_plasma_advanced.py`` fails if the installed
# binding stops matching this table.
_PLASMA_ADVANCED_TEMPLATE: dict[str, Any] = {
    "parameterClass": "PlasmaEtchingParameters",
    "constructorOverload": "parameters",
    "blocks": {
        "ions": {"native": "Ions", "fields": [
            advanced_number("meanEnergy", 100.0), advanced_number("sigmaEnergy", 10.0),
            advanced_number("exponent", 500.0), advanced_number("n_l", 10.0),
            advanced_number("minAngle", 1.3962634, maximum=math.pi, unit="rad"),
            advanced_number("inflectAngle", 1.55334303, maximum=math.pi, unit="rad"),
            advanced_number("thetaRMin", 1.2217304763960306, maximum=math.pi, unit="rad"),
            advanced_number("thetaRMax", 1.5707963267948966, maximum=math.pi, unit="rad"),
        ]},
        "mask": {"native": "Mask", "fields": [
            advanced_number("A_sp", 0.0139), advanced_number("B_sp", 9.3),
            advanced_number("Eth_sp", 20.0), advanced_number("rho", 500.0, minimum=0.000001),
        ]},
        "passivation": {"native": "Passivation", "fields": [
            advanced_number("A_ie", 3.0), advanced_number("Eth_ie", 10.0),
        ]},
        "polymer": {"native": "Polymer", "fields": [
            advanced_number("A_sp", 0.02), advanced_number("B_sp", 8.5),
            advanced_number("Eth_sp", 15.0), advanced_number("rho", 5.0, minimum=0.000001),
            advanced_number("a1", -0.26, minimum=-1_000_000.0), advanced_number("a2", 2.72, minimum=-1_000_000.0),
            advanced_number("a3", -4.3, minimum=-1_000_000.0), advanced_number("a4", 1.95, minimum=-1_000_000.0),
            advanced_boolean("usePolyCosThetaYield", False),
        ]},
        # Substrate.B_ie is declared by the native struct but unused by the
        # CPU surface model (the angle-dependent branch is commented out), so
        # it is deliberately not exposed.
        "substrate": {"native": "Substrate", "fields": [
            advanced_number("A_ie", 7.0), advanced_number("A_sp", 0.0337),
            advanced_number("B_sp", 9.3),
            advanced_number("Eth_ie", 15.0), advanced_number("Eth_sp", 20.0),
            advanced_number("beta_sigma", 0.04), advanced_number("k_sigma", 300.0),
            advanced_number("rho", 5.02, minimum=0.000001),
        ]},
    },
    "scalars": [advanced_number("passivationFlux", 100.0)],
    "maps": [
        {"key": "beta_E", "native": "beta_E", "default": 1.0, "minimum": 0.0, "maximum": 1.0},
        {"key": "beta_P", "native": "beta_P", "default": 1.0, "minimum": 0.0, "maximum": 1.0},
        {"key": "rateFactors", "native": "rateFactors", "default": 1.0, "minimum": 0.0, "maximum": 1_000_000.0},
    ],
}

_CF4O2_ADVANCED: dict[str, Any] = {
    "parameterClass": "CF4O2Parameters",
    "constructorOverload": "parameters",
    "blocks": {
        "ions": {"native": "Ions", "fields": [
            advanced_number("meanEnergy", 100.0), advanced_number("sigmaEnergy", 10.0),
            advanced_number("exponent", 500.0), advanced_number("n_l", 10.0),
            advanced_number("minAngle", 1.3962634, maximum=math.pi, unit="rad"),
            advanced_number("inflectAngle", 1.55334303, maximum=math.pi, unit="rad"),
        ]},
        "mask": {"native": "Mask", "fields": [
            advanced_number("A_sp", 0.0139), advanced_number("Eth_sp", 20.0),
            advanced_number("rho", 500.0, minimum=0.000001),
        ]},
        "passivation": {"native": "Passivation", "fields": [
            advanced_number("A_C_ie", 3.0), advanced_number("A_O_ie", 3.0),
            advanced_number("Eth_C_ie", 10.0), advanced_number("Eth_O_ie", 10.0),
        ]},
        "si": {"native": "Si", "fields": [
            advanced_number("A_ie", 7.0), advanced_number("A_sp", 0.0337),
            advanced_number("Eth_ie", 15.0), advanced_number("Eth_sp", 20.0),
            advanced_number("beta_sigma", 0.04), advanced_number("k_sigma", 300.0),
            advanced_number("rho", 5.02, minimum=0.000001),
        ]},
        # The native CF4O2 surface model only consumes SiGe.beta_sigma,
        # SiGe.k_sigma and SiGe.rho. A_ie/A_sp/Eth_ie/Eth_sp are ignored in the
        # SiGe branch, and x only computes the initial k_sigma default (it does
        # not recompute it when changed), so those fields are not exposed.
        "siGe": {"native": "SiGe", "fields": [
            advanced_number("beta_sigma", 0.04), advanced_number("k_sigma", 345.168420976798),
            advanced_number("rho", 4.84, minimum=0.000001),
        ]},
    },
    "scalars": [advanced_boolean("fluxIncludeSticking", False)],
    "maps": [
        {"key": name, "native": name, "default": 1.0, "minimum": 0.0, "maximum": 1.0}
        for name in ("gamma_C", "gamma_C_oxidized", "gamma_F", "gamma_F_oxidized",
                     "gamma_O", "gamma_O_passivated")
    ],
}

_FLAT_MAPPING_SF6 = {
    "ionFlux": "ionFlux", "etchantFlux": "etchantFlux", "oxygenFlux": "passivationFlux",
    "meanIonEnergy": "Ions.meanEnergy", "sigmaIonEnergy": "Ions.sigmaEnergy",
    "ionExponent": "Ions.exponent", "oxySputterYield": "Passivation.A_ie",
    "etchStopDepth": "etchStopDepth",
}
_FLAT_MAPPING_SF6C4F8 = {
    "ionFlux": "ionFlux", "etchantFlux": "etchantFlux",
    "meanEnergy": "Ions.meanEnergy", "sigmaEnergy": "Ions.sigmaEnergy",
    "ionExponent": "Ions.exponent", "etchStopDepth": "etchStopDepth",
}
_FLAT_MAPPING_CF4O2 = {
    "ionFlux": "ionFlux", "etchantFlux": "etchantFlux",
    "oxygenFlux": "oxygenFlux", "polymerFlux": "polymerFlux",
    "meanIonEnergy": "Ions.meanEnergy", "sigmaIonEnergy": "Ions.sigmaEnergy",
    "ionExponent": "Ions.exponent", "oxySputterYield": "Passivation.A_O_ie",
    "polySputterYield": "Passivation.A_C_ie", "etchStopDepth": "etchStopDepth",
}


def _apply_flat_defaults(advanced: dict[str, Any], fields: list[dict[str, Any]],
                         flat_mapping: dict[str, str]) -> None:
    """Make shared advanced defaults equal the flat catalog defaults.

    ``defaultSource`` distinguishes a value the workbench already sends through
    the flat overload (``flat``) from a native parameter-class default with no
    flat equivalent (``nativeParameterClass``/``nativeMaterialMap``).
    """
    flat_defaults = {field["key"]: field["default"] for field in fields}
    targets: dict[str, dict[str, Any]] = {}
    for block in advanced["blocks"].values():
        for field in block["fields"]:
            targets[f'{block["native"]}.{field["key"]}'] = field
    for field in advanced["scalars"]:
        targets[field["key"]] = field
    for flat_key, native_path in flat_mapping.items():
        target = targets.get(native_path)
        if target is None:
            continue
        target["default"] = flat_defaults[flat_key]
        target["flatKey"] = flat_key
        target["defaultSource"] = "flat"
    for block in advanced["blocks"].values():
        for field in block["fields"]:
            field.setdefault("defaultSource", "nativeParameterClass")
    for field in advanced["scalars"]:
        field.setdefault("defaultSource", "nativeParameterClass")
    for row in advanced["maps"]:
        row.setdefault("defaultSource", "nativeMaterialMap")


def _plasma_advanced(fields: list[dict[str, Any]], flat_mapping: dict[str, str], *,
                     drop_blocks: tuple[str, ...] = (), drop_scalars: tuple[str, ...] = (),
                     drop_maps: tuple[str, ...] = (), default_patches: dict[str, float] | None = None,
                     rejected: dict[str, str] | None = None) -> dict[str, Any]:
    """Clone the shared PlasmaEtchingParameters schema for one model."""
    advanced = deepcopy(_PLASMA_ADVANCED_TEMPLATE)
    advanced["blocks"] = {name: block for name, block in advanced["blocks"].items()
                          if name not in drop_blocks}
    advanced["scalars"] = [field for field in advanced["scalars"]
                           if field["key"] not in drop_scalars]
    advanced["maps"] = [row for row in advanced["maps"] if row["key"] not in drop_maps]
    advanced["flatMapping"] = dict(flat_mapping)
    _apply_flat_defaults(advanced, fields, flat_mapping)
    by_block = {block["native"]: block for block in advanced["blocks"].values()}
    by_scalar = {field["key"]: field for field in advanced["scalars"]}
    for path, value in (default_patches or {}).items():
        if "." in path:
            block_name, field_name = path.split(".", 1)
            next(field for field in by_block[block_name]["fields"] if field["key"] == field_name)["default"] = value
        else:
            by_scalar[path]["default"] = value
    if rejected:
        advanced["rejectedKeys"] = dict(rejected)
    return advanced


def _cf4o2_advanced() -> dict[str, Any]:
    advanced = deepcopy(_CF4O2_ADVANCED)
    advanced["flatMapping"] = dict(_FLAT_MAPPING_CF4O2)
    _apply_flat_defaults(advanced, CF4O2_FLAT_FIELDS, _FLAT_MAPPING_CF4O2)
    return advanced


# ``steps[].advancedOxidation`` is a top-level optional object on a
# specialized:oxidation step, analogous to ``steps[].surfaceDiffusion``. Every
# row below is a setter that was proven on the installed 4.6.2 binding to
# change the returned surface deterministically; see
# docs/oxidation-advanced-contract.md for the probe evidence. Defaults are the
# installed native defaults, so materialising the block is a behaviour no-op.
OXIDATION_ADVANCED_FIELDS: list[dict[str, Any]] = [
    {"key": "cflFactor", "label": "CFL 系数", "kind": "number", "default": 0.499,
     "minimum": 0.01, "maximum": 0.499, "step": 0.001, "nativeUnit": None,
     "nativeSetter": "Oxidation.setCFLFactor",
     "minimumNote": "installed native clamps to [0.001, 0.499]; the Workbench floor is 0.01 to keep bounded-child runs tractable"},
    {"key": "mechanicsTolerance", "label": "力学收敛容差", "kind": "number", "default": 0.01,
     "minimum": 0.01, "maximum": 0.2, "step": 0.001, "nativeUnit": None,
     "nativeSetter": "Oxidation.setMechanicsTolerance",
     "minimumNote": "values below the installed default can stop the mechanics coupling from converging; only looser values are accepted"},
    {"key": "couplingTolerance", "label": "扩散-变形耦合容差", "kind": "number", "default": 0.05,
     "minimum": 0.05, "maximum": 0.5, "step": 0.001, "nativeUnit": None,
     "nativeSetter": "Oxidation.setCouplingTolerance",
     "minimumNote": "must be at least mechanicsTolerance; the native validator warns when it is tighter"},
    {"key": "simplePressureRelaxation", "label": "SIMPLE 压力松弛", "kind": "number", "default": 0.5,
     "minimum": 0.01, "maximum": 1.0, "step": 0.01, "nativeUnit": None,
     "nativeSetter": "Oxidation.setSimplePressureRelaxation"},
]

OXIDATION_ADVANCED_NATIVE_CLASS = "Oxidation"
OXIDATION_ADVANCED_LOCATION = "steps[].advancedOxidation"


def oxidation_advanced_spec() -> dict[str, Any]:
    return {
        "location": OXIDATION_ADVANCED_LOCATION,
        "nativeClass": OXIDATION_ADVANCED_NATIVE_CLASS,
        "requiresNativeModel": "oxidation",
        "objectShape": "strict object; unknown keys are rejected",
        "defaultsSource": "installed viennaps 4.6.2 Oxidation member defaults; re-probed on 4.7.0",
        "runtimeGate": {key: reason for key, reason in OXIDATION_ADVANCED_RESTRICTED.items()},
        "fields": deepcopy(OXIDATION_ADVANCED_FIELDS),
        "excluded": [
            {"member": "setGpuMode", "reason": "CPU-only batch; GPU oxidation has no host evidence"},
            {"member": "setGpuPreconditioner", "reason": "GPU-only"},
            {"member": "setMaxGridPoints", "reason": "a solver cap, not a physics control; below the solve region it raises RuntimeError, above it is inert"},
            {"member": "setSolveBounds / clearSolveBounds", "reason": "raw Cartesian index bounds; a controlled probe hit 'Cartesian solve region is empty' at valid-looking bounds"},
            {"member": "setCouplingIterations", "reason": "explicit default 30 is inert; lowering it to 1 fails to converge"},
            {"member": "setMechanicsIterations", "reason": "explicit default 200 is inert; lowering it to 1 fails to converge"},
            {"member": "setPressureIterations / setStokesIterations", "reason": "explicit defaults 500 are inert; lowering pressure to 1 fails to converge"},
            {"member": "setPressureTolerance / setStokesTolerance", "reason": "changing them requires a coupled mechanicsTolerance adjustment; not exposed as independent knobs"},
            {"member": "setSimpleVelocityRelaxation", "reason": "no surface change above 1e-8 um in the controlled 2D/3D probes"},
            {"member": "setMaskParameters and setMask* mechanics setters", "reason": "no verified LOCOS output on this host; the controlled 2D probe collapsed the oxide level set, so mask mechanics stays unexposed"},
            {"member": "estimatePlanarOxideThickness", "reason": "read-only helper, not a setter; used as a native readback"},
            {"member": "OxidationParameters / psOxidation / psProcessContext", "reason": "not bound by the installed Python module"},
        ],
    }


CATALOG: list[dict[str, Any]] = [
    {"id": "sf6o2", "label": "SF₆/O₂ 等离子体刻蚀", "nativeClass": "SF6O2Etching", "operation": "etch", "usesFlux": True,
     "optionalKeys": ["advanced", "coverageTolerance"],
     "optionalFields": [coverage_tolerance_field()],
     "advanced": _plasma_advanced(PLASMA_FLAT_FIELDS, _FLAT_MAPPING_SF6),
     "fields": list(PLASMA_FLAT_FIELDS)},
    {"id": "cf4o2", "label": "CF₄/O₂ 等离子体刻蚀", "nativeClass": "CF4O2Etching", "operation": "etch", "usesFlux": True,
     "optionalKeys": ["advanced", "coverageTolerance"],
     "optionalFields": [coverage_tolerance_field()],
     "advanced": _cf4o2_advanced(),
     "fields": list(CF4O2_FLAT_FIELDS)},
    {"id": "hbro2", "label": "HBr/O₂ 等离子体刻蚀", "nativeClass": "HBrO2Etching", "operation": "etch", "usesFlux": True,
     "optionalKeys": ["advanced", "coverageTolerance"],
     "optionalFields": [coverage_tolerance_field()],
     # HBrO2Etching's native defaults differ from PlasmaEtchingParameters():
     # Substrate.A_ie/A_sp/Eth_sp are 5.0/0.5/30.0 instead of 7.0/0.0337/20.0.
     "advanced": _plasma_advanced(PLASMA_FLAT_FIELDS, _FLAT_MAPPING_SF6, default_patches={
         "Substrate.A_ie": 5.0, "Substrate.A_sp": 0.5, "Substrate.Eth_sp": 30.0}),
     "fields": list(PLASMA_FLAT_FIELDS)},
    {"id": "sf6c4f8", "label": "SF₆/C₄F₈ 等离子体刻蚀", "nativeClass": "SF6C4F8Etching", "operation": "etch", "usesFlux": True,
     "optionalKeys": ["advanced", "coverageTolerance"],
     "optionalFields": [coverage_tolerance_field()],
     # SF6C4F8Etching has no passivation particle: its surface model
     # dereferences a missing "passivationFlux" flux scalar when the value is
     # positive (SIGSEGV). The model also does not read the Passivation block or
     # beta_P, so those are absent from this model's allowlist.
     "advanced": _plasma_advanced(
         SF6C4F8_FLAT_FIELDS, _FLAT_MAPPING_SF6C4F8,
         drop_blocks=("passivation",), drop_scalars=("passivationFlux",), drop_maps=("beta_P",),
         rejected={"passivationFlux": "SF6C4F8Etching has no passivation particle; a positive value "
                                     "dereferences a missing native flux scalar and can SIGSEGV"}),
     "fields": list(SF6C4F8_FLAT_FIELDS)},
    {"id": "fluorocarbon", "label": "Fluorocarbon 等离子体刻蚀", "nativeClass": "FluorocarbonEtching", "operation": "etch", "usesFlux": True,
     "fields": [number("ionFlux", 1), number("etchantFlux", 1), number("polyFlux", 0.2),
                number("k_ie", 2), number("k_ev", 2), number("delta_p", 1), number("temperature", 300),
                number("meanEnergy", 100), number("sigmaEnergy", 10), number("exponent", 500), number("n_l", 10),
                number("inflectAngle", 1.55334303), number("minAngle", 1.3962634),
                number("etchStopDepth", -1_000_000, minimum=-1_000_000),
                {"key": "materialParametersByMaterial", "label": "原生材料参数表", "kind": "fluorocarbonMaterials",
                 "defaults": FLUOROCARBON_MATERIAL_DEFAULTS, "minimum": -1_000_000.0, "maximum": 1_000_000.0,
                 "step": 0.01, "nativeUnit": None}]},
    {"id": "ionBeam", "label": "离子束刻蚀（IBE）", "nativeClass": "IonBeamEtching", "operation": "etch", "usesFlux": True,
     "fields": ibe_fields()},
    # Experimental: native construction and CPU runs are covered by
    # tests/test_engine_batch1_specialized.py, but no Stage 8/9 direct-reference
    # acceptance row has been archived for this model yet. Validation warns
    # instead of claiming verified CPU coverage; GPU coverage stays unclaimed.
    # ViennaPS 4.6.2/4.7.0 binds FaradayCageEtching in both dimensions. 2D is
    # exposed only behind the strict CPU_DISK engine gate below: the probe in
    # worker/probe_faraday_2d.py completes every 2D cage/tilt combination on
    # CPU_DISK, while AUTO/GPU hangs for part of the same matrix.
    {"id": "faradayCage", "label": "法拉第笼离子束刻蚀", "nativeClass": "FaradayCageEtching", "operation": "etch",
     "usesFlux": True, "experimental": True, "dimensions": [2, 3],
     "fluxEnginesByDimension": {"2": ["CPU_DISK"], "3": None},
     "fields": [number("cageAngle", 0, maximum=180)] + ibe_fields()},
    {"id": "wetEtch", "label": "晶向湿法刻蚀", "nativeClass": "WetEtching", "operation": "etch", "usesFlux": False,
     "fields": [rates("materialRates", 0.0),
                {"key": "useCrystallineRates", "label": "使用晶向速率", "kind": "boolean", "default": False},
                {"key": "direction100", "label": "direction100 [x,y,z]", "kind": "vector3", "default": [1, 0, 0]},
                {"key": "direction010", "label": "direction010 [x,y,z]", "kind": "vector3", "default": [0, 1, 0]},
                number("rate100", 0.1), number("rate110", 0.1), number("rate111", 0.1), number("rate311", 0.1)]},
    {"id": "teosDeposition", "label": "TEOS 热沉积", "nativeClass": "TEOSDeposition", "operation": "deposit", "usesFlux": True,
     "depositMaterial": "SiO2", "fields": [number("stickingProbabilityP1", 0.5, maximum=1), number("rateP1", 0.05), number("orderP1", 1),
                number("stickingProbabilityP2", 0, maximum=1), number("rateP2", 0), number("orderP2", 0)]},
    {"id": "teospecvd", "label": "TEOS PECVD", "nativeClass": "TEOSPECVD", "operation": "deposit", "usesFlux": True,
     "depositMaterial": "SiO2", "fields": [number("stickingProbabilityRadical", 0.5, maximum=1), number("depositionRateRadical", 0.05),
                number("depositionRateIon", 0.02), number("exponentIon", 1), number("stickingProbabilityIon", 1, maximum=1),
                number("reactionOrderRadical", 1), number("reactionOrderIon", 1), number("minAngleIon", 0, maximum=180)]},
    {"id": "ald", "label": "单粒子 ALD", "nativeClass": "SingleParticleALD", "operation": "deposit", "usesFlux": True,
     "depositMaterialSelectable": True, "fields": [number("stickingProbability", 0.5, maximum=1), number("gasMeanFreePath", 1, minimum=-1),
                number("growthPerCycle", 0.02), number("incomingFlux", 1), number("evaporationFlux", 0.1),
                number("s0", 1), number("coverageDiffusionCoefficient", 0), number("pulseTime", 0.02),
                number("purgePulseTime", 0), number("coverageTimeStep", 0.01), number("purgeTimeStep", 1),
                number("numCycles", 1, minimum=1, maximum=10000, step=1)]},
    {"id": "oxidation", "label": "硅热氧化", "nativeClass": "Oxidation", "operation": "native", "usesFlux": False,
     "advancedOxidation": oxidation_advanced_spec(),
     "fields": [enum("oxidant", "Dry", [("Dry", "Dry · O₂"), ("Wet", "Wet · H₂O")], label="氧化剂"),
                enum("orientation", "Si100", [("Si100", "Si (100)"), ("Si110", "Si (110)"),
                                             ("Si111", "Si (111)"), ("PolySi", "多晶硅")], label="晶向"),
                number("temperatureC", 1000, minimum=800, maximum=1200, step=1, unit="°C"),
                number("pressureAtm", 1, minimum=0.000001, unit="atm"), number("timeHours", 0.001, minimum=0.000003, maximum=0.1666667, unit="h"),
                number("timeStepHours", 0.0001, minimum=0.000001, unit="h"), number("transferCoefficientUmHr", 0.1, unit="µm/hr"),
                number("initialOxideThicknessUm", 0, unit="µm"), number("reactionActivationVolumeM3", 0, unit="m³"),
                number("diffusionActivationVolumeM3", 0, unit="m³"),
                {"key": "maskMaterial", "label": "掩膜材料", "kind": "materialOptional", "default": ""},
                {"key": "siliconMaterial", "label": "硅材料", "kind": "material", "default": "Si"},
                {"key": "oxideMaterial", "label": "氧化层材料", "kind": "material", "default": "SiO2"}]},
    {"id": "oxideRegrowth", "label": "氧化层再生与再沉积", "nativeClass": "OxideRegrowth", "operation": "native", "usesFlux": False,
     "requiresCellSet": True, "fields": [number("nitrideEtchRate", 0.01), number("oxideEtchRate", 0.005),
                number("redepositionRate", 0.001), number("redepositionThreshold", 0.01), number("redepositionTimeInt", 0.1),
                number("diffusionCoefficient", 0.02), number("sinkStrength", 0.1), number("scallopVelocity", 0.05),
                number("centerVelocity", 0.05), number("topHeight", 0.6), number("centerWidth", 1.2),
                number("stabilityFactor", 0.15)]},
    {"id": "selectiveEpitaxy", "label": "选择性外延", "nativeClass": "SelectiveEpitaxy", "operation": "deposit", "usesFlux": False,
     "depositMaterial": "SiGe", "optionalKeys": ["nvFactors"], "optionalFields": [NV_FACTORS_FIELD],
     "fields": [rates("materialRates", 0.05, label="外延材料速率表"),
                number("rate111", 0.5), number("rate100", 1.0)]},
    # GeometricTrenchDeposition is a ViennaLS geometric-advection distribution,
    # not a velocity-field model: it deposits on the top level set (created by
    # the step's selectable depositMaterial) without a native material table.
    # 4.6.2 ignores trenchWidth, and its thickness does not scale with the
    # process duration. It evaluates the local radius from the absolute z
    # coordinate, with the trench top assumed at z=0. Validation surfaces these
    # native semantics as a warning instead of pretending they are ordinary
    # deposition parameters. The audit's 3D SIGSEGV was reproduced as a
    # parameter-reach failure that also occurs in 2D: a rate below
    # max(a+b, bottomMed) can leave an empty deposited level set and eventually
    # SIGSEGV. With the shared reach guard enforced, 2D and 3D use the same
    # native class and all parametric geometry kinds are exercised. GDS remains gated
    # because its vertical span is not described by the project.
    {"id": "geometricTrenchDeposition", "label": "几何沟槽沉积", "nativeClass": "GeometricTrenchDeposition",
     "operation": "deposit", "usesFlux": False, "experimental": True, "dimensions": [2, 3],
     "depositMaterialSelectable": True,
     "fields": [number("trenchWidth", 1.0, minimum=0.000001),
                number("trenchDepth", 4.0, minimum=0.000001),
                number("depositionRate", 0.5, minimum=0.000001),
                number("bottomMed", 0.2, minimum=0),
                number("a", 0.3), number("b", 0.1),
                number("n", 1.0, minimum=1, maximum=100, step=1)]},
    {"id": "neutralTransport", "label": "中性粒子输运", "nativeClass": "NeutralTransport", "operation": "native", "usesFlux": True,
     "fields": [number("incomingFlux", 0.2), number("sourceDistributionPower", 1), number("etchFrontSticking", 0.5, maximum=1),
                number("zeroCoverageSticking", 0.1, maximum=1), number("coverageTimeStep", 0.05),
                number("desorptionRate", 0), number("surfaceDiffusionCoefficient", 0), number("surfaceSiteDensity", 1.66e-5),
                {"key": "useSteadyStateCoverage", "label": "稳态覆盖率", "kind": "boolean", "default": True},
                {"key": "etchFrontMaterial", "label": "刻蚀前沿材料", "kind": "material", "default": "Si"},
                {"key": "fluxLabel", "label": "fluxLabel", "kind": "text", "default": "neutralFlux"},
                {"key": "coverageLabel", "label": "coverageLabel", "kind": "text", "default": "neutralCoverage"}]},
]

BY_ID = {row["id"]: row for row in CATALOG}


def specialized_catalog() -> dict[str, Any]:
    return {"schemaVersion": 1, "engineVersion": engine_version(), "unitsNote": "安装版绑定未声明单位的数值按原样传入；只对原生文档明确标注单位的氧化字段显示单位。",
            "models": deepcopy(CATALOG)}


def model_spec(model_id: str) -> dict[str, Any] | None:
    return BY_ID.get(model_id)


def advanced_spec(model_id: str) -> dict[str, Any] | None:
    """Return the strict ``nativeParameters.advanced`` schema for a model."""
    spec = BY_ID.get(model_id)
    return spec.get("advanced") if spec else None
