"""Pluggable per-process step model registry.

Why this exists: a monolithic ``if step.mechanism == ...`` adapter makes every new
process model touch the same 1000-line file. Separating **process step dispatch**
from **engine state representation** lets an author write a 20-line model for one
specific process (e.g. ``etch/anisotropic`` or custom ``model="cmp_dishing_v2"``)
in its own module or external plugin file.

Resolution precedence for a :class:`StepIR`:

1. Explicit custom model override ``step.model`` (when set and registered);
2. Exact ``(step.kind, step.mechanism)`` handler;
3. Wildcard kind ``("*", step.mechanism)`` handler;
4. Otherwise raises ``NotImplementedError`` -> recorded as ``unsupported`` by the
   runner, never silently skipped.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from engines.protocol import JobSpec, StepIR

StateT = TypeVar("StateT")


@dataclass
class StepModelContext:
    """Read-only context passed to every step model execution."""

    job: JobSpec
    out_dir: Path
    step_index: int
    polygon_cache: dict[int, list[list[tuple[float, float]]]] = field(default_factory=dict)
    converted_gds: Path | None = None

    def layer_polygons(self, layer: int) -> list[list[tuple[float, float]]]:
        """Return polygons in local window coordinates (um) for ``layer``."""
        layer = int(layer)
        if layer in self.polygon_cache:
            return self.polygon_cache[layer]
        layout_cfg = self.job.layout or {}
        raw_path = layout_cfg.get("path")
        if not raw_path:
            raise NotImplementedError("step requires job.layout.path")
        from engines.layout import read_gds_polygons, to_gds

        target = Path(raw_path)
        if target.suffix.lower() in (".oas", ".oasis"):
            if self.converted_gds is None:
                cached = self.out_dir / "layout.gds"
                self.converted_gds = cached if cached.is_file() else to_gds(target, cached)
            target = self.converted_gds
        _, _, all_polys = read_gds_polygons(target)
        ox, oy = (self.job.domain.origin_xy or [0.0, 0.0])[:2]
        ox, oy = float(ox), float(oy)
        by_layer: dict[int, list[list[tuple[float, float]]]] = {}
        for poly_layer, _dtype, points in all_polys:
            shifted = [(float(x) - ox, float(y) - oy) for x, y in points]
            if len(shifted) >= 3:
                by_layer.setdefault(int(poly_layer), []).append(shifted)
        for k, v in by_layer.items():
            self.polygon_cache.setdefault(k, v)
        return self.polygon_cache.get(layer, [])


StepModelFn = Callable[[StateT, StepIR, StepModelContext], dict[str, Any] | None]


class StepModelRegistry(Generic[StateT]):
    """Dispatch table mapping ``(kind, mechanism)`` and custom ``model`` IDs to kernels."""

    def __init__(self, engine_id: str) -> None:
        self.engine_id = engine_id
        self._by_pair: dict[tuple[str, str], tuple[str, StepModelFn[StateT]]] = {}
        self._by_model: dict[str, StepModelFn[StateT]] = {}

    def register(self, kind: str, mechanism: str, *, model_id: str | None = None):
        """Decorator registering a handler for ``(kind, mechanism)``."""
        resolved_id = model_id or f"{kind}/{mechanism}"

        def _decorator(fn: StepModelFn[StateT]) -> StepModelFn[StateT]:
            self._by_pair[(kind, mechanism)] = (resolved_id, fn)
            self._by_model.setdefault(resolved_id, fn)
            return fn

        return _decorator

    def register_model(self, model_id: str):
        """Decorator registering a named process-specific custom model."""
        if not model_id or not model_id.strip():
            raise ValueError("model_id must be non-empty")

        def _decorator(fn: StepModelFn[StateT]) -> StepModelFn[StateT]:
            self._by_model[model_id] = fn
            return fn

        return _decorator

    def supported_mechanisms(self) -> tuple[str, ...]:
        mechs = {"initial"} | {mech for (_kind, mech) in self._by_pair}
        if self._by_model:
            mechs.add("custom")
        return tuple(sorted(mechs))

    def registered_models(self) -> list[str]:
        return sorted(self._by_model)

    def resolve(self, step: StepIR) -> tuple[str, StepModelFn[StateT]]:
        if step.model:
            if step.model in self._by_model:
                return step.model, self._by_model[step.model]
            if step.mechanism == "custom":
                raise NotImplementedError(
                    f"engine {self.engine_id!r} has no custom step model {step.model!r}; "
                    f"available: {self.registered_models()}"
                )
        pair = self._by_pair.get((step.kind, step.mechanism))
        if pair is not None:
            return pair
        wildcard = self._by_pair.get(("*", step.mechanism))
        if wildcard is not None:
            return wildcard
        raise NotImplementedError(
            f"{step.kind}/{step.mechanism} has no lowering registered on engine {self.engine_id!r}"
        )

    def load_external_plugins(self, env_var: str = "VIENNAPS_STEP_MODEL_MODULES") -> list[str]:
        """Load extra step-model modules from ``env_var`` (comma/colon separated).

        Each entry may be a Python dotted module name or a ``.py`` file path exposing
        ``register_models(registry)``.
        """
        raw = os.environ.get(env_var, "").strip()
        if not raw:
            return []
        loaded: list[str] = []
        sep = "," if "," in raw else os.pathsep
        for token in (part.strip() for part in raw.split(sep) if part.strip()):
            path = Path(token)
            if path.suffix == ".py" and path.is_file():
                spec = importlib.util.spec_from_file_location(
                    f"_ext_step_model_{path.stem}", path
                )
                if spec is None or spec.loader is None:
                    raise RuntimeError(f"cannot load step-model plugin from {path}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            else:
                module = importlib.import_module(token)
            hook = getattr(module, "register_models", None)
            if callable(hook):
                hook(self)
            loaded.append(token)
        return loaded
