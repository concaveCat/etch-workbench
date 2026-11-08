"""P4-1 hierarchical modules, typed variables and deterministic expansion.

The execution contract stays frozen: a :class:`FlowDefinitionV2` is expanded
into an authored :class:`FlowRevisionV1` whose flat steps are compiled by the
existing ``compile_flow`` path. Nothing here submits a run or changes
``ProjectV7``.

Module steps reuse the ``StepV7`` field set. A module step may additionally be
a nested module call (``{"id": ..., "moduleId": ..., "parameters": {...}}``),
which lets modules compose while keeping cycles and nesting depth explicit.

Expressions are deliberately small: numbers, variable names, ``+ - * / **``,
parentheses, unary minus and the ``min``/``max``/``abs``/``round`` functions.
They are evaluated against a Python ``ast`` whitelist; attributes, subscripts,
comprehensions and arbitrary calls are rejected. String fields may embed
``${expr}`` placeholders. A lone placeholder keeps the evaluated type; mixed
text is stringified.
"""

from __future__ import annotations

import ast
import math
import re
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from ..models import SolverV7, StepV7, StrictModel
from .ids import STEP_ID_POLICY, sha256_hex, stable_step_id
from .models import FlowRevisionV1, ModuleRevisionRefV1, ParameterProvenanceV1

MAX_EXPANDED_STEPS = 500
MAX_EXPANSION_DEPTH = 24
MAX_REPEAT_ITERATIONS = 50
MAX_MODULE_STEPS = 100
MAX_EXPRESSION_LENGTH = 512
MAX_EXPRESSION_NODES = 64
MAX_POWER_EXPONENT = 64
MAX_INTEGER_BITS = 256

Identifier = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
FlowId = Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
VariableName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
ScalarValue = float | int | str | bool

PLACEHOLDER = re.compile(r"\$\{([^{}]*)\}")
FULL_PLACEHOLDER = re.compile(r"^\$\{([^{}]*)\}$")
AUTHORED_ID_FORBIDDEN = re.compile(r"[^A-Za-z0-9_-]")


class ModuleError(ValueError):
    """Base class for P4-1 validation and expansion failures."""


class ExpressionError(ModuleError):
    """An expression is syntactically or semantically invalid."""


class UnknownVariableError(ExpressionError):
    """An expression references a variable that is not in scope."""


class UnknownModuleError(ModuleError):
    """A module-call references a module that is not registered."""


class UnknownModuleRevisionError(UnknownModuleError):
    """A module-call pins a revision that has not been retained."""


class ExpansionLimitError(ModuleError):
    """Expansion exceeded the step, depth or repeat limits."""


class ParameterBindingError(ModuleError):
    """Module parameters cannot be bound from the call site."""


# ---------------------------------------------------------------------------
# Safe expression evaluation
# ---------------------------------------------------------------------------

_BINARY_OPERATORS = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
    ast.Pow: lambda left, right: left**right,
}
_ALLOWED_FUNCTIONS = {
    "min": (min, 1, None),
    "max": (max, 1, None),
    "abs": (abs, 1, 1),
    "round": (round, 1, 2),
}


def _expression_error(expression: str, message: str) -> ExpressionError:
    return ExpressionError(f"invalid expression {expression!r}: {message}")


def expression_variables(expression: str) -> list[str]:
    """Return the variable names referenced by one expression."""

    tree = _parse_expression(expression)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in _ALLOWED_FUNCTIONS:
            if node.id not in names:
                names.append(node.id)
    return names


def _parse_expression(expression: str) -> ast.Expression:
    if not isinstance(expression, str) or not expression.strip():
        raise ExpressionError("expression is empty")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise _expression_error(expression, f"expression exceeds {MAX_EXPRESSION_LENGTH} characters")
    try:
        tree = ast.parse(expression, mode="eval")
        nodes = list(ast.walk(tree))
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise _expression_error(expression, getattr(exc, "msg", "expression is too deeply nested")) from exc
    if len(nodes) > MAX_EXPRESSION_NODES:
        raise _expression_error(expression, f"expression exceeds {MAX_EXPRESSION_NODES} syntax nodes")
    return tree


def _check_integer(expression: str, value: Any) -> Any:
    if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > MAX_INTEGER_BITS:
        raise _expression_error(expression, f"integer result exceeds {MAX_INTEGER_BITS} bits")
    return value


def _numeric(expression: str, value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _expression_error(expression, f"expected a number but got {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise _expression_error(expression, "non-finite number")
    return _check_integer(expression, value)


def _check_result(expression: str, value: Any) -> ScalarValue:
    if isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise _expression_error(expression, "result is not finite")
        return _check_integer(expression, value)
    raise _expression_error(expression, f"unsupported result type {type(value).__name__}")


def _eval_node(expression: str, node: ast.AST, variables: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(expression, node.body, variables)
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _expression_error(expression, "only numeric literals are allowed")
        if isinstance(value, float) and not math.isfinite(value):
            raise _expression_error(expression, "non-finite number")
        return _check_integer(expression, value)
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise UnknownVariableError(
                f"invalid expression {expression!r}: unknown variable {node.id!r}"
            )
        value = variables[node.id]
        if isinstance(value, (list, dict, tuple)) or value is None:
            raise _expression_error(expression, f"variable {node.id!r} is not a scalar")
        if isinstance(value, float) and not math.isfinite(value):
            raise _expression_error(expression, "non-finite number")
        return _check_integer(expression, value)
    if isinstance(node, ast.BinOp):
        operation = _BINARY_OPERATORS.get(type(node.op))
        if operation is None:
            raise _expression_error(expression, f"unsupported operator {type(node.op).__name__}")
        left = _numeric(expression, _eval_node(expression, node.left, variables))
        right = _numeric(expression, _eval_node(expression, node.right, variables))
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_POWER_EXPONENT:
            raise _expression_error(expression, f"exponent exceeds {MAX_POWER_EXPONENT}")
        if isinstance(node.op, ast.Div) and right == 0:
            raise _expression_error(expression, "division by zero")
        try:
            return _check_integer(expression, operation(left, right))
        except ZeroDivisionError as exc:
            raise _expression_error(expression, "division by zero") from exc
        except (OverflowError, MemoryError) as exc:
            raise _expression_error(expression, "arithmetic overflow") from exc
    if isinstance(node, ast.UnaryOp):
        operand = _numeric(expression, _eval_node(expression, node.operand, variables))
        if isinstance(node.op, ast.USub):
            return _check_integer(expression, -operand)
        if isinstance(node.op, ast.UAdd):
            return operand
        raise _expression_error(expression, f"unsupported unary operator {type(node.op).__name__}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
            raise _expression_error(expression, "only min, max, abs and round may be called")
        if node.keywords:
            raise _expression_error(expression, "keyword arguments are not supported")
        function, minimum, maximum = _ALLOWED_FUNCTIONS[node.func.id]
        if len(node.args) < minimum or (maximum is not None and len(node.args) > maximum):
            raise _expression_error(expression, f"{node.func.id} received the wrong argument count")
        arguments = [_eval_node(expression, argument, variables) for argument in node.args]
        try:
            return _check_integer(expression, function(*arguments))
        except (TypeError, ValueError, OverflowError, MemoryError) as exc:
            raise _expression_error(expression, str(exc)) from exc
    raise _expression_error(expression, f"unsupported syntax {type(node).__name__}")


def evaluate_expression(expression: str, variables: Mapping[str, Any]) -> ScalarValue:
    """Evaluate one whitelisted arithmetic expression against ``variables``."""

    tree = _parse_expression(expression)
    try:
        return _check_result(expression, _eval_node(expression, tree, variables))
    except (RecursionError, MemoryError) as exc:
        raise _expression_error(expression, "expression is too deeply nested") from exc


def _resolve_text(value: str, variables: Mapping[str, Any]) -> tuple[ScalarValue, str, str | None, list[str]]:
    """Resolve one string, returning value plus provenance metadata."""

    full = FULL_PLACEHOLDER.match(value.strip())
    if full is not None:
        inner = full.group(1).strip()
        resolved = evaluate_expression(inner, variables)
        references = expression_variables(inner)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", inner):
            return resolved, "variable", inner, references
        return resolved, "expression", inner, references

    if PLACEHOLDER.search(value) is None:
        return value, "literal", None, []

    references: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        references.extend(expression_variables(inner))
        return str(evaluate_expression(inner, variables))

    return PLACEHOLDER.sub(substitute, value), "expression", value, references


def resolve_value(value: Any, variables: Mapping[str, Any]) -> Any:
    """Deep-resolve placeholders in a JSON-like value."""

    if isinstance(value, str):
        return _resolve_text(value, variables)[0]
    if isinstance(value, list):
        return [resolve_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: resolve_value(item, variables) for key, item in value.items()}
    return value


def _collect_references(value: Any, into: set[str]) -> None:
    if isinstance(value, str):
        for match in PLACEHOLDER.finditer(value):
            into.update(expression_variables(match.group(1).strip()))
        return
    if isinstance(value, list):
        for item in value:
            _collect_references(item, into)
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_references(item, into)


# ---------------------------------------------------------------------------
# Module and flow contracts
# ---------------------------------------------------------------------------


class ModuleParameterV1(StrictModel):
    name: VariableName
    default: ScalarValue | None = None
    unit: Annotated[str, Field(max_length=32)] | None = None
    description: Annotated[str, Field(max_length=512)] = ""
    required: bool = False

    @model_validator(mode="after")
    def default_or_required(self) -> "ModuleParameterV1":
        if self.required and self.default is not None:
            raise ValueError(f"required parameter {self.name!r} must not declare a default")
        if not self.required and self.default is None:
            raise ValueError(f"parameter {self.name!r} must declare a default or be required")
        return self


class ModuleV1(StrictModel):
    """A reusable step group with a typed parameter interface."""

    schemaVersion: Literal[1] = 1
    moduleId: Identifier
    name: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(max_length=2000)] = ""
    parameters: Annotated[list[ModuleParameterV1], Field(max_length=64)] = Field(default_factory=list)
    steps: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=MAX_MODULE_STEPS)]

    @model_validator(mode="after")
    def validate_interface(self) -> "ModuleV1":
        names = [parameter.name for parameter in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("module parameter names must be unique")
        step_ids: list[str] = []
        for index, step in enumerate(self.steps):
            if not isinstance(step, dict):
                raise ValueError(f"module step {index} must be a mapping")
            step_id = step.get("id")
            if not isinstance(step_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", step_id):
                raise ValueError(f"module step {index} requires an id matching [A-Za-z0-9_-]{{1,64}}")
            step_ids.append(step_id)
            is_nested_call = "moduleId" in step
            if is_nested_call:
                if not isinstance(step["moduleId"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,63}", step["moduleId"]):
                    raise ValueError(f"module step {step_id!r} has an invalid moduleId")
                if len(step_id) < 3:
                    raise ValueError(
                        f"nested module-call step {step_id!r} needs a 3-64 character instance id"
                    )
                parameters = step.get("parameters", {})
                if not isinstance(parameters, dict):
                    raise ValueError(f"module step {step_id!r} parameters must be a mapping")
                revision = step.get("moduleRevision")
                if revision is not None and (
                    isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
                ):
                    raise ValueError(f"module step {step_id!r} moduleRevision must be a positive integer")
            elif "model" not in step:
                raise ValueError(f"module step {step_id!r} must declare a model or a moduleId")
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("module step ids must be unique")
        return self


class FlowVariableV1(StrictModel):
    name: VariableName
    default: ScalarValue
    unit: Annotated[str, Field(max_length=32)] | None = None
    description: Annotated[str, Field(max_length=512)] = ""


class FlowNodeStepV1(StrictModel):
    kind: Literal["step"]
    step: dict[str, Any]


class FlowNodeModuleCallV1(StrictModel):
    kind: Literal["module-call"]
    instanceId: Identifier
    moduleId: Identifier
    moduleRevision: Annotated[int, Field(ge=1)] | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class FlowNodeRepeatV1(StrictModel):
    kind: Literal["repeat"]
    repeatId: Identifier
    loopVariable: VariableName = "i"
    count: Annotated[int, Field(ge=1, le=MAX_REPEAT_ITERATIONS)] | None = None
    values: Annotated[list[ScalarValue], Field(min_length=1, max_length=MAX_REPEAT_ITERATIONS)] | None = None
    body: Annotated[list["FlowNodeV1"], Field(min_length=1)]

    @model_validator(mode="after")
    def exactly_one_driver(self) -> "FlowNodeRepeatV1":
        if (self.count is None) == (self.values is None):
            raise ValueError("repeat requires exactly one of count or values")
        if self.loopVariable == "index":
            raise ValueError("loopVariable 'index' is reserved for the iteration index")
        return self


FlowNodeV1 = Annotated[
    FlowNodeStepV1 | FlowNodeModuleCallV1 | FlowNodeRepeatV1,
    Field(discriminator="kind"),
]

FlowNodeRepeatV1.model_rebuild()


class FlowDefinitionV2(StrictModel):
    """A flow with variables, literal steps, module calls and repeat loops."""

    schemaVersion: Literal[2] = 2
    definitionId: Identifier
    flowId: FlowId
    revision: Annotated[int, Field(ge=1)]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(max_length=2000)] = ""
    variables: Annotated[list[FlowVariableV1], Field(max_length=64)] = Field(default_factory=list)
    nodes: Annotated[list[FlowNodeV1], Field(min_length=1)]
    solver: SolverV7

    @model_validator(mode="after")
    def validate_definition(self) -> "FlowDefinitionV2":
        names = [variable.name for variable in self.variables]
        if len(names) != len(set(names)):
            raise ValueError("flow variable names must be unique")
        if self.solver.random.mode != "fixed":
            raise ValueError("P4-1 FlowDefinition requires a fixed random seed")
        for node in self.nodes:
            if isinstance(node, FlowNodeStepV1):
                step_id = node.step.get("id")
                if not isinstance(step_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", step_id):
                    raise ValueError(
                        "flow step nodes require an authored id matching [A-Za-z0-9_-]{8,64}"
                    )
        return self


class ResolvedVariableV1(StrictModel):
    name: VariableName
    value: ScalarValue
    unit: Annotated[str, Field(max_length=32)] | None = None
    source: Literal["default", "override"]


class ModuleInstanceV1(StrictModel):
    instancePath: Annotated[str, Field(min_length=1, max_length=256)]
    moduleId: Identifier
    moduleRevision: Annotated[int, Field(ge=1)]
    parameters: dict[str, ScalarValue] = Field(default_factory=dict)


class IterationV1(StrictModel):
    path: Annotated[str, Field(min_length=1, max_length=256)]
    variable: VariableName
    index: Annotated[int, Field(ge=0, le=MAX_REPEAT_ITERATIONS - 1)]
    value: ScalarValue


class ExpandedStepV1(StrictModel):
    authoredStepId: Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    stepId: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    flowPath: Annotated[str, Field(min_length=1, max_length=512)]
    moduleInstanceIdPath: Annotated[str, Field(min_length=1, max_length=256)]
    iterationKey: Annotated[str, Field(min_length=1, max_length=32, pattern=r"^i[0-9]+$")]
    moduleId: Identifier | None = None
    moduleInstanceId: Identifier | None = None
    moduleRevision: Annotated[int, Field(ge=1)] | None = None
    step: dict[str, Any]
    parameterProvenance: dict[str, ParameterProvenanceV1] = Field(default_factory=dict)
    variableRefs: list[str] = Field(default_factory=list)


class ExpandedFlowV1(StrictModel):
    definitionId: Identifier
    flowId: FlowId
    revision: Annotated[int, Field(ge=1)]
    stepIdPolicy: str = STEP_ID_POLICY
    steps: list[ExpandedStepV1]
    stepCount: Annotated[int, Field(ge=0)]
    variables: list[ResolvedVariableV1] = Field(default_factory=list)
    iterations: list[IterationV1] = Field(default_factory=list)
    moduleInstances: list[ModuleInstanceV1] = Field(default_factory=list)
    moduleRevisions: list[ModuleRevisionRefV1] = Field(default_factory=list)
    maxDepth: Annotated[int, Field(ge=0)]


def flow_definition_pins(definition: FlowDefinitionV2) -> list[tuple[str, int]]:
    """Return every explicit ``(moduleId, moduleRevision)`` pin in a definition."""

    pins: set[tuple[str, int]] = set()

    def walk(nodes: Iterable[FlowNodeV1]) -> None:
        for node in nodes:
            if isinstance(node, FlowNodeModuleCallV1):
                if node.moduleRevision is not None:
                    pins.add((node.moduleId, node.moduleRevision))
            elif isinstance(node, FlowNodeRepeatV1):
                walk(node.body)

    walk(definition.nodes)
    return sorted(pins)


def module_step_pins(module: ModuleV1) -> list[tuple[str, int]]:
    """Return explicit module revision pins contained in a module's steps."""

    pins: set[tuple[str, int]] = set()
    for step in module.steps:
        if "moduleId" not in step:
            continue
        revision = step.get("moduleRevision")
        if revision is not None:
            pins.add((str(step["moduleId"]), int(revision)))
    return sorted(pins)


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------


class _Scope:
    __slots__ = ("variables", "instance_path", "iteration_key", "flow_path", "depth", "module_stack")

    def __init__(
        self,
        *,
        variables: dict[str, Any],
        instance_path: str,
        iteration_key: str,
        flow_path: str,
        depth: int,
        module_stack: tuple[str, ...],
    ):
        self.variables = variables
        self.instance_path = instance_path
        self.iteration_key = iteration_key
        self.flow_path = flow_path
        self.depth = depth
        self.module_stack = module_stack


def _authored_step_id(instance_path: str, step_id: str) -> str:
    """Build a deterministic, globally unique authored id for one expansion."""

    if instance_path == "root":
        return step_id
    segments = [segment for segment in instance_path.split("/")[1:] if segment]
    stem = AUTHORED_ID_FORBIDDEN.sub("_", "__".join([*segments, step_id]))
    if len(stem) > 64:
        digest = sha256_hex([instance_path, step_id])[:8]
        stem = f"{stem[:55]}__{digest}"
    if len(stem) < 8:
        stem = f"{stem}_{sha256_hex([instance_path, step_id])[:8]}"
    return stem


class _Expansion:
    def __init__(
        self,
        definition: FlowDefinitionV2,
        modules: Mapping[str, ModuleV1],
        *,
        max_steps: int,
        max_depth: int,
    ):
        self.definition = definition
        self.modules = modules
        self.max_steps = max_steps
        self.max_depth = max_depth
        self.steps: list[ExpandedStepV1] = []
        self.iterations: list[IterationV1] = []
        self.module_instances: list[ModuleInstanceV1] = []
        self.module_revisions: set[tuple[str, int]] = set()
        self.max_seen_depth = 0

    # -- step level ---------------------------------------------------------

    def add_step(
        self,
        raw_step: dict[str, Any],
        scope: _Scope,
        *,
        module_id: str | None,
        module_instance_id: str | None,
        module_revision: int | None = None,
    ) -> None:
        if len(self.steps) >= self.max_steps:
            raise ExpansionLimitError(
                f"expanded flow exceeds the {self.max_steps} step limit"
            )
        original_id = raw_step.get("id")
        if not isinstance(original_id, str):
            raise ModuleError(f"step at {scope.flow_path} has no authored id")
        authored_id = _authored_step_id(scope.instance_path, original_id)
        flow_path = f"{scope.flow_path}/{original_id}"
        if len(flow_path) > 512:
            raise ModuleError(f"expanded flow path is too long at {flow_path[:64]}…")

        resolved = resolve_value(raw_step, scope.variables)
        resolved["id"] = authored_id
        try:
            validated = TypeAdapter(StepV7).validate_python(resolved)
        except ValidationError as exc:
            raise ModuleError(f"step {flow_path} is invalid after expansion: {exc}") from exc
        payload = validated.model_dump(mode="json", exclude_none=True)

        provenance: dict[str, ParameterProvenanceV1] = {}
        references: set[str] = set()
        for key, value in payload.items():
            if key in {"id", "name", "enabled"}:
                continue
            raw_value = raw_step.get(key)
            if isinstance(raw_value, str):
                _, kind, source, field_refs = _resolve_text(raw_value, scope.variables)
            else:
                field_refs = set()
                _collect_references(raw_value, field_refs)
                kind = "expression" if field_refs else "literal"
                source = None
            references.update(field_refs)
            provenance[key] = ParameterProvenanceV1(
                kind=kind, source=source, resolvedValue=value
            )
        _collect_references(raw_step, references)

        step_id = stable_step_id(
            flow_id=self.definition.flowId,
            module_instance_id_path=scope.instance_path,
            authored_step_id=authored_id,
            iteration_key=scope.iteration_key,
        )
        if any(existing.stepId == step_id for existing in self.steps):
            raise ModuleError(
                f"expanded step id {step_id!r} collides at {flow_path}; instance paths must be unique"
            )
        self.steps.append(
            ExpandedStepV1(
                authoredStepId=authored_id,
                stepId=step_id,
                flowPath=flow_path,
                moduleInstanceIdPath=scope.instance_path,
                iterationKey=scope.iteration_key,
                moduleId=module_id,
                moduleInstanceId=module_instance_id,
                moduleRevision=module_revision,
                step=payload,
                parameterProvenance=provenance,
                variableRefs=sorted(references),
            )
        )

    # -- node level ---------------------------------------------------------

    def walk(self, nodes: Iterable[Any], scope: _Scope) -> None:
        for node in nodes:
            if isinstance(node, FlowNodeStepV1):
                self.add_step(node.step, scope, module_id=None, module_instance_id=None)
            elif isinstance(node, FlowNodeModuleCallV1):
                self.expand_module_call(node, scope)
            elif isinstance(node, FlowNodeRepeatV1):
                self.expand_repeat(node, scope)
            else:  # pragma: no cover - discriminated union keeps this unreachable
                raise ModuleError(f"unsupported flow node {type(node).__name__}")

    def expand_module_call(self, node: FlowNodeModuleCallV1, scope: _Scope) -> None:
        if scope.depth + 1 > self.max_depth:
            raise ExpansionLimitError(
                f"module nesting at {scope.flow_path} exceeds the depth limit {self.max_depth}"
            )
        versions = self.modules.get(node.moduleId)
        if not versions:
            raise UnknownModuleError(
                f"unknown module {node.moduleId!r} referenced at {scope.flow_path}"
            )
        if node.moduleRevision is None:
            revision = max(versions)
            module = versions[revision]
        else:
            revision = node.moduleRevision
            module = versions.get(revision)
            if module is None:
                raise UnknownModuleRevisionError(
                    f"module {node.moduleId!r} has no retained revision {revision} "
                    f"(available: {sorted(versions)}) referenced at {scope.flow_path}"
                )
        if node.moduleId in scope.module_stack:
            chain = " -> ".join([*scope.module_stack, node.moduleId])
            raise ModuleError(f"module cycle detected: {chain}")

        parameters = self.bind_parameters(module, node.parameters, scope)
        instance_path = f"{scope.instance_path}/{node.instanceId}"
        if len(instance_path) > 256:
            raise ModuleError(
                f"module instance path at {scope.flow_path} exceeds 256 characters; use shorter instance ids"
            )
        instance_scope = _Scope(
            variables={**scope.variables, **parameters},
            instance_path=instance_path,
            iteration_key=scope.iteration_key,
            flow_path=f"{scope.flow_path}/{node.instanceId}",
            depth=scope.depth + 1,
            module_stack=(*scope.module_stack, node.moduleId),
        )
        self.max_seen_depth = max(self.max_seen_depth, instance_scope.depth)
        self.module_revisions.add((module.moduleId, revision))
        self.module_instances.append(
            ModuleInstanceV1(
                instancePath=instance_path,
                moduleId=node.moduleId,
                moduleRevision=revision,
                parameters=parameters,
            )
        )
        for entry in module.steps:
            if "moduleId" in entry:
                nested = FlowNodeModuleCallV1(
                    kind="module-call",
                    instanceId=entry["id"],
                    moduleId=entry["moduleId"],
                    moduleRevision=entry.get("moduleRevision"),
                    parameters=entry.get("parameters", {}),
                )
                self.expand_module_call(nested, instance_scope)
            else:
                self.add_step(
                    entry,
                    instance_scope,
                    module_id=module.moduleId,
                    module_instance_id=node.instanceId,
                    module_revision=revision,
                )

    def expand_repeat(self, node: FlowNodeRepeatV1, scope: _Scope) -> None:
        if scope.depth + 1 > self.max_depth:
            raise ExpansionLimitError(
                f"repeat nesting at {scope.flow_path} exceeds the depth limit {self.max_depth}"
            )
        values: list[ScalarValue]
        if node.values is not None:
            values = list(node.values)
        else:
            assert node.count is not None
            values = list(range(node.count))
        for index, value in enumerate(values):
            iteration_path = f"{scope.instance_path}/{node.repeatId}/i{index}"
            if len(iteration_path) > 256:
                raise ModuleError(
                    f"repeat instance path at {scope.flow_path} exceeds 256 characters; use shorter repeat ids"
                )
            iteration_scope = _Scope(
                variables={**scope.variables, node.loopVariable: value, "index": index},
                instance_path=iteration_path,
                iteration_key=f"i{index}",
                flow_path=f"{scope.flow_path}/{node.repeatId}/i{index}",
                depth=scope.depth + 1,
                module_stack=scope.module_stack,
            )
            self.max_seen_depth = max(self.max_seen_depth, iteration_scope.depth)
            self.iterations.append(
                IterationV1(
                    path=iteration_path,
                    variable=node.loopVariable,
                    index=index,
                    value=value,
                )
            )
            self.walk(node.body, iteration_scope)

    # -- parameters ---------------------------------------------------------

    def bind_parameters(
        self, module: ModuleV1, call_parameters: Mapping[str, Any], scope: _Scope
    ) -> dict[str, ScalarValue]:
        declared = {parameter.name: parameter for parameter in module.parameters}
        unknown = set(call_parameters) - set(declared)
        if unknown:
            raise ParameterBindingError(
                f"module {module.moduleId!r} received undeclared parameters: {sorted(unknown)}"
            )
        bound: dict[str, ScalarValue] = {}
        for name, parameter in declared.items():
            if name in call_parameters:
                value = resolve_value(call_parameters[name], scope.variables)
                if isinstance(value, (list, dict, tuple)) or value is None:
                    raise ParameterBindingError(
                        f"parameter {name!r} of module {module.moduleId!r} must resolve to a scalar"
                    )
                bound[name] = value
            elif parameter.default is not None:
                bound[name] = parameter.default
            elif parameter.required:
                raise ParameterBindingError(
                    f"module {module.moduleId!r} requires parameter {name!r}"
                )
            else:  # pragma: no cover - model validation rejects this combination
                raise ParameterBindingError(
                    f"parameter {name!r} of module {module.moduleId!r} has no default"
                )
        return bound


def _module_catalog(
    modules: Iterable[ModuleV1]
    | Mapping[str, ModuleV1]
    | Mapping[str, Mapping[int, ModuleV1]],
) -> dict[str, dict[int, ModuleV1]]:
    """Normalize a module set or revision history into ``moduleId -> revisions``.

    A plain list/mapping is treated as revision 1, which keeps direct library
    callers compatible. The store passes its full retained history so pinned
    revisions resolve without consulting "latest".
    """

    catalog: dict[str, dict[int, ModuleV1]] = {}
    if isinstance(modules, Mapping):
        for module_id, value in modules.items():
            if isinstance(value, Mapping):
                catalog[module_id] = {int(revision): module for revision, module in value.items()}
            else:
                catalog[module_id] = {1: value}
        return catalog
    for module in modules:
        catalog[module.moduleId] = {1: module}
    return catalog


def _resolved_variables(
    definition: FlowDefinitionV2, overrides: Mapping[str, Any]
) -> tuple[dict[str, ScalarValue], list[ResolvedVariableV1]]:
    declared = {variable.name: variable for variable in definition.variables}
    unknown = set(overrides) - set(declared)
    if unknown:
        raise UnknownVariableError(
            f"unknown flow variables {sorted(unknown)}; declared variables are {sorted(declared)}"
        )
    values: dict[str, ScalarValue] = {}
    resolved: list[ResolvedVariableV1] = []
    for name, variable in declared.items():
        if name in overrides:
            value = overrides[name]
            if isinstance(value, (list, dict, tuple)) or value is None:
                raise UnknownVariableError(f"variable {name!r} override must be a scalar")
            source = "override"
        else:
            value = variable.default
            source = "default"
        values[name] = value
        resolved.append(
            ResolvedVariableV1(name=name, value=value, unit=variable.unit, source=source)
        )
    return values, resolved


def expand_flow_definition(
    definition: FlowDefinitionV2,
    modules: Iterable[ModuleV1]
    | Mapping[str, ModuleV1]
    | Mapping[str, Mapping[int, ModuleV1]],
    *,
    variables: Mapping[str, Any] | None = None,
    max_steps: int = MAX_EXPANDED_STEPS,
    max_depth: int = MAX_EXPANSION_DEPTH,
) -> ExpandedFlowV1:
    """Expand a :class:`FlowDefinitionV2` into flat authored steps.

    The result is deterministic for a fixed definition, module set, variable
    set and limits. Step ids never include parameter values.
    """

    definition = definition.model_copy(deep=True)
    catalog = _module_catalog(modules)
    value_map, resolved_variables = _resolved_variables(definition, variables or {})
    expansion = _Expansion(definition, catalog, max_steps=max_steps, max_depth=max_depth)
    root = _Scope(
        variables=dict(value_map),
        instance_path="root",
        iteration_key="i0",
        flow_path="root",
        depth=0,
        module_stack=(),
    )
    expansion.walk(definition.nodes, root)
    if not expansion.steps:
        raise ModuleError("expanded flow produced no steps")
    authorised = [step.authoredStepId for step in expansion.steps]
    if len(authorised) != len(set(authorised)):
        raise ModuleError("expanded flow produced duplicate authored step ids")
    return ExpandedFlowV1(
        definitionId=definition.definitionId,
        flowId=definition.flowId,
        revision=definition.revision,
        steps=expansion.steps,
        stepCount=len(expansion.steps),
        variables=resolved_variables,
        iterations=expansion.iterations,
        moduleInstances=expansion.module_instances,
        moduleRevisions=[
            ModuleRevisionRefV1(moduleId=module_id, revision=revision)
            for module_id, revision in sorted(expansion.module_revisions)
        ],
        maxDepth=expansion.max_seen_depth,
    )


def to_flow_revision(
    definition: FlowDefinitionV2, expanded: ExpandedFlowV1
) -> tuple[FlowRevisionV1, dict[str, tuple[str, str]]]:
    """Build the authored revision consumed by ``compile_flow``.

    Returns the flow plus the placement map (``authoredStepId`` to
    ``moduleInstanceIdPath``/``iterationKey``) that keeps the SourceMap stable.
    """

    adapter = TypeAdapter(StepV7)
    try:
        steps = [adapter.validate_python(step.step) for step in expanded.steps]
    except ValidationError as exc:
        raise ModuleError(f"expanded flow contains an invalid step: {exc}") from exc
    flow = FlowRevisionV1(
        schemaVersion=1,
        flowId=expanded.flowId,
        revision=expanded.revision,
        name=definition.name,
        steps=steps,
        solver=definition.solver.model_copy(deep=True),
    )
    placements = {
        step.authoredStepId: (step.moduleInstanceIdPath, step.iterationKey)
        for step in expanded.steps
    }
    return flow, placements
