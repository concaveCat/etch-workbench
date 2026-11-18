"""Translate an SPX reference flow (exported Python) into StepIR steps.

Fixes over Phase A:
* ``patterned`` resets to ``False`` when a blanket ``depo_fill``, ``depo_isotropic``,
  or ``etch_cmp`` buries/planarises the wafer, preventing later blanket etchback
  steps (such as Memory1 step 32 ``etch_aniso_TiN_W``) from being misclassified as
  litho-masked etches;
* ``mask`` never lists a material that is itself being etched in ``rates``;
* Assigns honest ``comparability`` grades instead of marking every step ``exact``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

from engines.normalize import canonical
from engines.protocol import DomainSpec, InitialStructure, JobSpec, StepIR

SUPPORTED = {
    "substrate", "depo_fill", "depo_isotropic", "etch_isotropic", "etch_anisotropic",
    "etch_ellipsoidal", "etch_strip", "etch_cmp", "litho_patterning",
}
IGNORED = {"route", "clip", "layout", "flow", "module", "material", "signature",
           "modify", "close", "file", "save", "banner", "show"}


class TranslationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Call:
    name: str
    kwargs: dict[str, Any]


def _unit(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func = ast.unparse(node.func)
        if func.endswith("Unit") and node.args and isinstance(node.args[0], ast.Constant):
            return str(node.args[0].value)
    return None


def _value(node: ast.AST) -> Any:
    unit = _unit(node)
    if unit is not None:
        return ("unit", unit)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Tuple):
        return tuple(_value(elt) for elt in node.elts)
    if isinstance(node, ast.List):
        return [_value(elt) for elt in node.elts]
    if isinstance(node, ast.Dict):
        return {(_value(k) if not isinstance(k, ast.Constant) else k.value): _value(v)
                for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_value(node.operand)
    raise TranslationError(f"cannot evaluate {ast.unparse(node)!r}")


def parse_calls(text: str) -> list[Call]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith(("def ", "import ", "from ")):
                tree = ast.parse("\n".join(lines[index:]))
                break
        else:
            raise
    calls: list[Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Attribute):
            continue
        if not ast.unparse(func.value).endswith("procedural"):
            continue
        kwargs = {kw.arg: _value(kw.value) for kw in node.keywords if kw.arg}
        calls.append(Call(func.attr, kwargs))
    return calls


def _amount(value: Any) -> float:
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], tuple):
        return float(value[0])
    if isinstance(value, (int, float)):
        return float(value)
    raise TranslationError(f"not an amount: {value!r}")


def _rate(value: Any) -> float:
    if isinstance(value, tuple) and len(value) == 2:
        return float(value[0])
    raise TranslationError(f"not a rate: {value!r}")


def _filter_materials(filters: Any) -> list[str]:
    names: list[str] = []
    if not isinstance(filters, list):
        return names
    for clause in filters:
        if isinstance(clause, list):
            for term in clause:
                if isinstance(term, tuple) and len(term) == 3 and term[1] == "equals":
                    names.append(str(term[2]))
    return names


def _step_location(call: Call) -> dict[str, str]:
    return {str(k): str(v) for k, v in (call.kwargs.get("Location") or {}).items()}


def translate(
    text: str,
    *,
    case_id: str,
    window: list[float],
    grid_delta: float,
    layout_path: str,
    spx_layers: dict[str, int],
    domain_height: float,
) -> JobSpec:
    calls = [call for call in parse_calls(text) if call.name in SUPPORTED | IGNORED]
    substrate = next((call for call in calls if call.name == "substrate"), None)
    if substrate is None:
        raise TranslationError("the exported flow has no substrate step")
    initial = InitialStructure(
        kind="plane",
        substrate="Si",
        substrate_thickness=_amount(substrate.kwargs.get("Thickness")),
        films=(),
    )

    steps: list[StepIR] = [
        StepIR(step_id="s0_substrate", kind="initial", mechanism="initial", duration=0.0)
    ]
    present: set[str] = {initial.substrate}
    height = 0.0
    top_material = initial.substrate
    patterned = False
    active_layer: int | None = None

    def material(name: str) -> str:
        return canonical(name, "spx")

    for call in calls:
        if call.name in IGNORED:
            continue
        location = _step_location(call)
        step_id = f"{len(steps):02d}_{location.get('Step', call.name)}"
        if call.name == "substrate":
            continue
        if call.name == "depo_fill":
            materials = _filter_materials(call.kwargs.get("MaterialFilter"))
            if len(materials) != 1:
                raise TranslationError(f"{step_id}: depo_fill needs one material")
            target = material(materials[0])
            offset = _amount(call.kwargs.get("Offset"))
            steps.append(
                StepIR(
                    step_id=step_id,
                    kind="deposit",
                    mechanism="fill",
                    duration=0.0,
                    comparability="exact",
                    parameters={"material": target, "offset": offset},
                )
            )
            present.add(target)
            top_material = target
            patterned = False
            active_layer = None
            height += offset
            continue
        if call.name == "depo_isotropic":
            materials = _filter_materials(call.kwargs.get("MaterialFilter"))
            rate = _rate(call.kwargs.get("Rate"))
            if len(materials) != 1:
                raise TranslationError(f"{step_id}: depo_isotropic needs one material")
            target = material(materials[0])
            duration = _amount(call.kwargs.get("Time"))
            steps.append(
                StepIR(
                    step_id=step_id,
                    kind="deposit",
                    mechanism="isotropic",
                    duration=duration,
                    rates={target: rate},
                    comparability="exact",
                )
            )
            present.add(target)
            top_material = target
            patterned = False
            active_layer = None
            height += abs(rate) * duration
            continue
        if call.name in ("etch_isotropic", "etch_anisotropic"):
            rates: dict[str, float] = {}
            for entry in call.kwargs.get("MaterialDependent") or []:
                materials = _filter_materials(entry[0])
                if not materials:
                    continue
                rates[material(materials[0])] = -abs(_rate(entry[1]))
            if not rates:
                raise TranslationError(f"{step_id}: no material rates")
            mask = (
                [top_material]
                if (patterned and top_material in present and top_material not in rates)
                else []
            )
            params: dict[str, Any] = {"mask": mask, "patterned": patterned}
            if patterned and active_layer is not None:
                params["openings_layer"] = active_layer
            mech = "isotropic" if call.name == "etch_isotropic" else "anisotropic"
            steps.append(
                StepIR(
                    step_id=step_id,
                    kind="etch",
                    mechanism=mech,
                    duration=_amount(call.kwargs.get("Time")),
                    rates=rates,
                    anisotropy=1.0,
                    comparability="exact" if mech == "isotropic" else "approximate",
                    parameters=params,
                )
            )
            continue
        if call.name == "etch_ellipsoidal":
            ellip_rates: dict[str, list[float]] = {}
            for entry in call.kwargs.get("MaterialDependent") or []:
                materials = _filter_materials(entry[0])
                if not materials:
                    continue
                ellip_rates[material(materials[0])] = [
                    -abs(_rate(entry[1])),
                    -abs(_rate(entry[2])),
                ]
            mask = (
                [top_material]
                if (patterned and top_material in present and top_material not in ellip_rates)
                else []
            )
            params = {"rates": ellip_rates, "mask": mask, "patterned": patterned}
            if patterned and active_layer is not None:
                params["openings_layer"] = active_layer
            steps.append(
                StepIR(
                    step_id=step_id,
                    kind="etch",
                    mechanism="ellipsoidal",
                    duration=_amount(call.kwargs.get("Time")),
                    comparability="approximate",
                    parameters=params,
                )
            )
            continue
        if call.name == "etch_strip":
            materials = _filter_materials(call.kwargs.get("MaterialFilter"))
            targets = [material(name) for name in materials]
            steps.append(
                StepIR(
                    step_id=step_id,
                    kind="strip",
                    mechanism="strip",
                    duration=0.0,
                    comparability="exact",
                    parameters={"materials": targets},
                )
            )
            present.difference_update(targets)
            continue
        if call.name == "etch_cmp":
            stop = _filter_materials(call.kwargs.get("EtchstopMaterialsFilter"))
            steps.append(
                StepIR(
                    step_id=step_id,
                    kind="planarize",
                    mechanism="cmp",
                    duration=0.0,
                    comparability="approximate",
                    parameters={
                        "stop_material": material(stop[0]) if stop else None,
                        "offset": _amount(call.kwargs.get("Offset")),
                    },
                )
            )
            patterned = False
            active_layer = None
            continue
        if call.name == "litho_patterning":
            names = [str(name) for name in call.kwargs.get("LayerNames") or []]
            if len(names) != 1:
                raise TranslationError(f"{step_id}: litho needs one layer name")
            if names[0] not in spx_layers:
                raise TranslationError(f"{step_id}: unknown SPX layer {names[0]!r}")
            patterned = True
            active_layer = int(spx_layers[names[0]])
            top_material = "PMMA"
            steps.append(
                StepIR(
                    step_id=step_id,
                    kind="mask",
                    mechanism="litho",
                    duration=0.0,
                    comparability="approximate",
                    parameters={
                        "layer": active_layer,
                        "layer_name": names[0],
                        "thickness": 0.05,
                        "polarity": "negative",
                        "material": "PMMA",
                    },
                )
            )
            present.add("PMMA")
            height += 0.1
            continue
        raise TranslationError(f"{step_id}: {call.name} has no StepIR mapping")

    job = JobSpec(
        case_id=case_id,
        engine="any",
        domain=DomainSpec(
            width=float(window[2]),
            depth=float(window[3]),
            height=max(float(domain_height), round(height + 0.1, 3)),
            grid_delta=float(grid_delta),
            dimensions=3,
            origin_xy=[float(window[0]), float(window[1])],
        ),
        initial=initial,
        steps=tuple(steps),
        layout={"path": layout_path, "spxLayers": spx_layers},
    )
    job.validate()
    return job
