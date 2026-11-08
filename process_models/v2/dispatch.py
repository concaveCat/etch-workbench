"""Step dispatch decisions over one execution registry.

A compiled project step is intentionally resolved through the same
``ProcessSpec.parse_step`` hook for every execution kind.  This module only
answers *what kind of step is this and which spec claims it*; it does not
acquire an engine, prepare state, or execute anything.  The worker uses the
decision to choose the v2 path or an explicit legacy fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from pydantic import BaseModel

from .contract import ProcessSpec
from .registry import ProcessRegistry

__all__ = ["DispatchDecision", "DispatchKind", "dispatch_step", "normalize_plugin_step"]

DispatchKind = Literal["process", "field", "geometry", "legacy", "invalid"]


@dataclass(frozen=True)
class DispatchDecision:
    """The resolved kind, spec, and canonical params for one raw step.

    ``legacy`` means no registered v2 spec claimed the step; ``invalid`` means
    a spec claimed it but parsing or ambiguity failed.  An invalid step must
    not silently fall back to the legacy path.
    """

    kind: DispatchKind
    spec: ProcessSpec | None = None
    params: BaseModel | None = None
    reason: str = ""
    error: str | None = None

    @property
    def claimed(self) -> bool:
        """Return whether a v2 step spec owns this step."""
        return self.kind in ("process", "field", "geometry")


def normalize_plugin_step(step: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the raw step shape a process plugin's ``parse_step`` expects.

    ProjectV7 carries custom process steps as ``model="plugin"``,
    ``pluginId="sf6o2"``, ``params={...}``.  Existing plugins parse their
    original flat shape, so the platform expands the envelope here rather than
    requiring every plugin to know the project-schema wrapper.
    """
    if step.get("model") != "plugin":
        return step
    plugin_id = step.get("pluginId")
    params = step.get("params")
    if not isinstance(plugin_id, str) or not plugin_id:
        raise ValueError("plugin step requires a non-empty pluginId")
    if not isinstance(params, Mapping):
        raise ValueError("plugin step params must be an object")
    return {
        **dict(params),
        "id": step.get("id"),
        "name": step.get("name"),
        "enabled": step.get("enabled"),
        "model": plugin_id,
    }


def dispatch_step(
    registry: ProcessRegistry,
    project: Mapping[str, Any],
    step: Mapping[str, Any],
) -> DispatchDecision:
    """Resolve ``step`` without executing it.

    A registry miss is an explicit ``legacy`` decision.  A parse error or
    ambiguous match is ``invalid`` and carries the error text; callers must not
    treat it as a fallback candidate.
    """
    try:
        step = normalize_plugin_step(step)
    except ValueError as error:
        return DispatchDecision(kind="invalid", reason="plugin step envelope is invalid", error=str(error))
    model = step.get("model") if isinstance(step, Mapping) else None
    try:
        resolved = registry.resolve_step(project, step)
    except ValueError as error:
        return DispatchDecision(
            kind="invalid",
            reason=f"step model={model!r} failed v2 parameter parsing",
            error=str(error),
        )
    if resolved is None:
        return DispatchDecision(
            kind="legacy",
            reason=f"no registered v2 step spec claims model={model!r}",
        )
    spec, params = resolved
    kind = spec.execution_kind
    if kind not in ("process", "field", "geometry"):
        return DispatchDecision(
            kind="invalid",
            spec=spec,
            params=params,
            reason=f"spec {spec.id!r} has unknown execution_kind {kind!r}",
            error=f"unknown execution_kind {kind!r}",
        )
    return DispatchDecision(
        kind=kind,
        spec=spec,
        params=params,
        reason=f"claimed by {spec.id}@{spec.version} ({kind})",
    )
