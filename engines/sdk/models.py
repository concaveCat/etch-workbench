"""Pluggable per-process step model & concrete algorithm registry.

Separates **process step dispatch** and **concrete algorithm selection** from **engine
state representation**, and maintains full provenance metadata for every registered
algorithm (algorithm type, theoretical/contract origin, fidelity, source file:line).

Resolution precedence for a :class:`StepIR` on ``engine_id``:

1. Explicit per-step override ``step.model`` (when set and registered on this engine);
2. Per-job / CLI override ``job.algorithms`` (keys ``"<engine>:<kind>/<mechanism>"``,
   ``"<kind>/<mechanism>"``, or ``"<mechanism>"``);
3. Global editable default in ``engines/algorithms.json``
   (``active_defaults[engine_id]["<kind>/<mechanism>"]``);
4. Code-registered ``(step.kind, step.mechanism)`` or wildcard ``("*", step.mechanism)``;
5. Otherwise raises ``NotImplementedError`` -> recorded as ``unsupported`` by the
   runner, never silently skipped.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Generic, Sequence, TypeVar

from engines.protocol import JobSpec, StepIR

StateT = TypeVar("StateT")
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AlgorithmMetadata:
    """Self-describing record for one concrete step algorithm."""

    model_id: str
    slot: str | None
    algorithm_type: str = "custom"
    fidelity: str = "custom"
    origin: str = "custom_plugin"
    summary: str = ""
    source: str = ""
    aliases: tuple[str, ...] = ()

    def to_json(self, *, is_active: bool = False) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "slot": self.slot,
            "algorithm_type": self.algorithm_type,
            "fidelity": self.fidelity,
            "origin": self.origin,
            "summary": self.summary,
            "source": self.source,
            "aliases": list(self.aliases),
            "is_active": is_active,
        }


def _detect_source(fn: Callable[..., Any]) -> str:
    try:
        src_file = Path(inspect.getsourcefile(fn) or "").resolve()
        _, lineno = inspect.getsourcelines(fn)
        rel = src_file.relative_to(REPO_ROOT) if REPO_ROOT in src_file.parents else src_file
        return f"{rel}:{lineno}"
    except Exception:  # noqa: BLE001
        return getattr(fn, "__module__", "unknown")


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
    """Dispatch table mapping ``(kind, mechanism)`` and algorithm IDs to kernels."""

    def __init__(self, engine_id: str, *, use_global_defaults: bool = False) -> None:
        self.engine_id = engine_id
        self.use_global_defaults = use_global_defaults
        self._by_pair: dict[tuple[str, str], tuple[str, StepModelFn[StateT]]] = {}
        self._by_model: dict[str, StepModelFn[StateT]] = {}
        self._canonical_id: dict[str, str] = {}
        self._metadata: dict[str, AlgorithmMetadata] = {}

    def register(
        self,
        kind: str,
        mechanism: str,
        *,
        model_id: str | None = None,
        algorithm_type: str = "custom",
        fidelity: str = "custom",
        origin: str = "built_in",
        summary: str = "",
        aliases: Sequence[str] = (),
    ):
        """Decorator registering the primary handler for ``(kind, mechanism)``."""
        pair_alias = f"{kind}/{mechanism}"
        resolved_id = model_id or pair_alias
        extra_aliases = tuple(
            dict.fromkeys([*( [pair_alias] if resolved_id != pair_alias else [] ), *aliases])
        )

        def _decorator(fn: StepModelFn[StateT]) -> StepModelFn[StateT]:
            meta = AlgorithmMetadata(
                model_id=resolved_id,
                slot=pair_alias,
                algorithm_type=algorithm_type,
                fidelity=fidelity,
                origin=origin,
                summary=summary or (inspect.getdoc(fn) or "").splitlines()[0] if inspect.getdoc(fn) else summary,
                source=_detect_source(fn),
                aliases=extra_aliases,
            )
            self._by_pair[(kind, mechanism)] = (resolved_id, fn)
            self._by_model[resolved_id] = fn
            self._canonical_id[resolved_id] = resolved_id
            self._metadata[resolved_id] = meta
            for alias in extra_aliases:
                self._by_model.setdefault(alias, fn)
                self._canonical_id.setdefault(alias, resolved_id)
            return fn

        return _decorator

    def register_model(
        self,
        model_id: str,
        *,
        slot: str | None = None,
        algorithm_type: str = "custom",
        fidelity: str = "custom",
        origin: str = "custom_plugin",
        summary: str = "",
        aliases: Sequence[str] = (),
    ):
        """Decorator registering a named selectable or custom step algorithm."""
        if not model_id or not model_id.strip():
            raise ValueError("model_id must be non-empty")
        clean_aliases = tuple(dict.fromkeys(a for a in aliases if a and a != model_id))

        def _decorator(fn: StepModelFn[StateT]) -> StepModelFn[StateT]:
            doc_first = (inspect.getdoc(fn) or "").splitlines()[0] if inspect.getdoc(fn) else ""
            meta = AlgorithmMetadata(
                model_id=model_id,
                slot=slot,
                algorithm_type=algorithm_type,
                fidelity=fidelity,
                origin=origin,
                summary=summary or doc_first,
                source=_detect_source(fn),
                aliases=clean_aliases,
            )
            self._by_model[model_id] = fn
            self._canonical_id[model_id] = model_id
            self._metadata[model_id] = meta
            for alias in clean_aliases:
                self._by_model[alias] = fn
                self._canonical_id[alias] = model_id
            return fn

        return _decorator

    def has_model(self, model_id: str) -> bool:
        return model_id in self._by_model

    def supported_mechanisms(self) -> tuple[str, ...]:
        mechs = {"initial"} | {mech for (_kind, mech) in self._by_pair}
        if self._by_model:
            mechs.add("custom")
        return tuple(sorted(mechs))

    def registered_models(self) -> list[str]:
        return sorted(self._metadata)

    def describe_algorithms(self) -> list[dict[str, Any]]:
        from engines.algorithms import active_default

        rows: list[dict[str, Any]] = []
        for mid in sorted(self._metadata, key=lambda k: (self._metadata[k].slot or "zzz", k)):
            meta = self._metadata[mid]
            is_act = False
            if meta.slot and "/" in meta.slot:
                s_kind, s_mech = meta.slot.split("/", 1)
                cfg_mid = active_default(self.engine_id, s_kind, s_mech) if self.use_global_defaults else None
                if cfg_mid:
                    is_act = self._canonical_id.get(cfg_mid, cfg_mid) == mid
                else:
                    pair = self._by_pair.get((s_kind, s_mech))
                    is_act = pair is not None and pair[0] == mid
            rows.append(meta.to_json(is_active=is_act))
        return rows

    def _belongs_to_other_engine(self, model_id: str) -> bool:
        known_prefixes = ("voxel.", "viennaps.", "spx.")
        for prefix in known_prefixes:
            if model_id.startswith(prefix) and prefix[:-1] != self.engine_id:
                return True
        return False

    def resolve_with_metadata(
        self,
        step: StepIR,
        job_algorithms: dict[str, str] | None = None,
    ) -> tuple[str, StepModelFn[StateT], AlgorithmMetadata | None]:
        slot = f"{step.kind}/{step.mechanism}"

        # 1) Per-step explicit override (step.model):
        if step.model:
            if step.model in self._by_model:
                canon = self._canonical_id.get(step.model, step.model)
                return step.model, self._by_model[step.model], self._metadata.get(canon)
            if step.mechanism == "custom" or not self._belongs_to_other_engine(step.model):
                if step.mechanism == "custom":
                    raise NotImplementedError(
                        f"engine {self.engine_id!r} has no custom step model {step.model!r}; "
                        f"available: {self.registered_models()}"
                    )

        # 2) Per-job / CLI algorithm override (job.algorithms):
        if job_algorithms:
            scoped_key = f"{self.engine_id}:{slot}"
            if scoped_key in job_algorithms:
                req = job_algorithms[scoped_key]
                if req not in self._by_model:
                    raise NotImplementedError(
                        f"engine {self.engine_id!r} has no algorithm {req!r} for {slot!r}; "
                        f"available: {self.registered_models()}"
                    )
                canon = self._canonical_id.get(req, req)
                return req, self._by_model[req], self._metadata.get(canon)

            unscoped = job_algorithms.get(slot) or job_algorithms.get(step.mechanism)
            if unscoped and not self._belongs_to_other_engine(unscoped):
                if unscoped not in self._by_model:
                    raise NotImplementedError(
                        f"engine {self.engine_id!r} has no algorithm {unscoped!r} for {slot!r}; "
                        f"available: {self.registered_models()}"
                    )
                canon = self._canonical_id.get(unscoped, unscoped)
                return unscoped, self._by_model[unscoped], self._metadata.get(canon)

        # 3) Global editable active_defaults table (engines/algorithms.json):
        if self.use_global_defaults:
            from engines.algorithms import active_default

            cfg_mid = active_default(self.engine_id, step.kind, step.mechanism)
            if cfg_mid is not None:
                if cfg_mid not in self._by_model:
                    raise NotImplementedError(
                        f"engines/algorithms.json selects unknown algorithm {cfg_mid!r} for "
                        f"{self.engine_id}:{slot}; available: {self.registered_models()}"
                    )
                canon = self._canonical_id.get(cfg_mid, cfg_mid)
                return canon, self._by_model[cfg_mid], self._metadata.get(canon)

        # 4) Code-registered fallback pair:
        pair = self._by_pair.get((step.kind, step.mechanism))
        if pair is not None:
            return pair[0], pair[1], self._metadata.get(pair[0])
        wildcard = self._by_pair.get(("*", step.mechanism))
        if wildcard is not None:
            return wildcard[0], wildcard[1], self._metadata.get(wildcard[0])
        raise NotImplementedError(
            f"{step.kind}/{step.mechanism} has no lowering registered on engine {self.engine_id!r}"
        )

    def resolve(
        self, step: StepIR, job_algorithms: dict[str, str] | None = None
    ) -> tuple[str, StepModelFn[StateT]]:
        mid, fn, _meta = self.resolve_with_metadata(step, job_algorithms=job_algorithms)
        return mid, fn

    def load_external_plugins(self, env_var: str = "VIENNAPS_STEP_MODEL_MODULES") -> list[str]:
        """Load extra step-model modules from ``env_var`` (comma/colon separated)."""
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
