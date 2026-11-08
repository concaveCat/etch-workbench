"""P1 flow templates, scalar variables and deterministic substitution."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import Field

from ..models import StrictModel, StepV7
from pydantic import TypeAdapter

Id = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
PLACEHOLDER = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class TemplateVariableV1(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    value: float | int | str
    unit: str | None = None
    description: str = ""


class FlowTemplateV1(StrictModel):
    schemaVersion: Literal[1]
    templateId: Id
    name: Annotated[str, Field(min_length=1, max_length=80)]
    description: str = ""
    steps: list[dict[str, Any]]
    variables: list[TemplateVariableV1] = Field(default_factory=list)


def _resolve(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_resolve(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, variables) for key, item in value.items()}
    if isinstance(value, str):
        match = PLACEHOLDER.fullmatch(value.strip())
        if match:
            name = match.group(1)
            if name not in variables:
                raise ValueError(f"template variable {name!r} is not defined")
            return variables[name]
    return value


def resolve_template(template: FlowTemplateV1, overrides: dict[str, Any] | None = None) -> list[StepV7]:
    variables = {item.name: item.value for item in template.variables}
    variables.update(overrides or {})
    resolved = [_resolve(step, variables) for step in template.steps]
    return [TypeAdapter(StepV7).validate_python(step) for step in resolved]
