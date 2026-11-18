"""Base subprocess runner for stepwise simulation engines.

Isolates every cross-cutting concern so engine adapters stay small and honest:

* Domain & per-step limit checks (with cascade ``out_of_domain`` / ``unsupported``
  blocking so subsequent steps never execute on a diverged geometry);
* Strict separation of ``init_ms``, ``solve_ms``, ``frame_ms``, ``probe_ms``,
  ``section_ms``, and ``checkpoint_ms``;
* Optional skipping of frames (``job.emit_frames=False``) and checkpoints
  (``job.emit_checkpoints=False``) for pure solver speed benchmarks;
* ``--describe`` introspection flag for dynamic engine discovery.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from engines import limits as engine_limits
from engines.protocol import (
    EngineInfo,
    Frame,
    JobSpec,
    SectionSpec,
    StepIR,
    StepOutcome,
    write_json,
)
from engines.sdk.models import StepModelContext, StepModelRegistry

StateT = TypeVar("StateT")


class EngineAdapterBase(ABC, Generic[StateT]):
    """Lifecycle contract for a stepwise simulation engine."""

    engine_id: str
    models: StepModelRegistry[StateT]

    def __init__(self, models: StepModelRegistry[StateT]) -> None:
        self.models = models
        self.engine_id = models.engine_id

    @abstractmethod
    def info(self) -> EngineInfo:
        """Return self-describing engine metadata and version."""

    @abstractmethod
    def open_state(self, job: JobSpec, out_dir: Path) -> StateT:
        """Build the initial geometry (or restore from ``job.restore``)."""

    @abstractmethod
    def extract_frame(self, state: StateT, job: JobSpec, out_dir: Path, step_id: str) -> Frame:
        """Write and return one renderable frame for ``step_id``."""

    @abstractmethod
    def probe_points(self, state: StateT, points: list[list[float]]) -> list[str]:
        """Return canonical material labels at ``points``."""

    @abstractmethod
    def section_heights(
        self, state: StateT, spec: SectionSpec
    ) -> dict[str, list[float | None]]:
        """Return per-material interface heights along ``spec.columns``."""

    def save_checkpoint(self, state: StateT, out_dir: Path, step_id: str) -> str | None:
        """Persist optional per-step checkpoint; return path relative to ``out_dir``."""
        return None

    def engine_limits(self) -> dict[str, float]:
        return {}

    def run_job(self, job: JobSpec, out_dir: Path) -> dict[str, Any]:
        self.models.load_external_plugins()
        init_started = time.perf_counter()
        state = self.open_state(job, out_dir)
        init_ms = (time.perf_counter() - init_started) * 1000.0

        ctx = StepModelContext(job=job, out_dir=out_dir, step_index=0)
        probe_pts = list(job.probe.points) if job.probe is not None else []
        section_spec = job.section
        limits_override = self.engine_limits()

        blocked_by: str | None = None
        blocked_kind: str = "out_of_domain"
        total_solve_ms = 0.0
        outcomes: list[StepOutcome] = []

        for index, step in enumerate(job.steps):
            ctx.step_index = index
            if blocked_by is not None:
                status = "out_of_domain" if blocked_kind == "out_of_domain" else "skipped"
                outcomes.append(
                    StepOutcome(
                        step_id=step.step_id,
                        status=status,
                        wall_ms=0.0,
                        solve_ms=0.0,
                        reason=(
                            f"skipped: an earlier step ({blocked_by}) left this engine's "
                            f"domain ({blocked_kind})"
                        ),
                    )
                )
                continue

            if index != 0:
                reason, est = engine_limits.check(
                    self.engine_id, job, step, limits=limits_override
                )
                if reason is not None:
                    blocked_by = step.step_id
                    blocked_kind = "out_of_domain"
                    outcomes.append(
                        StepOutcome(
                            step_id=step.step_id,
                            status="out_of_domain",
                            wall_ms=0.0,
                            solve_ms=0.0,
                            metrics={"estimate": est},
                            reason=reason,
                        )
                    )
                    print(
                        json.dumps({"event": "step_out_of_domain", "stepId": step.step_id}),
                        flush=True,
                    )
                    continue

            solve_started = time.perf_counter()
            step_notes: dict[str, Any] = {}
            if index == 0 and step.mechanism == "initial":
                solve_ms = init_ms
                step_notes["model"] = "initial"
            else:
                try:
                    resolved_model, handler, algo_meta = self.models.resolve_with_metadata(
                        step, job_algorithms=job.algorithms
                    )
                    extra = handler(state, step, ctx)
                    if extra:
                        step_notes.update(extra)
                    step_notes.setdefault("model", resolved_model)
                    if algo_meta is not None:
                        step_notes.setdefault("algorithmType", algo_meta.algorithm_type)
                        step_notes.setdefault("algorithmFidelity", algo_meta.fidelity)
                except NotImplementedError as exc:
                    solve_ms = (time.perf_counter() - solve_started) * 1000.0
                    blocked_by = step.step_id
                    blocked_kind = "unsupported"
                    outcomes.append(
                        StepOutcome(
                            step_id=step.step_id,
                            status="unsupported",
                            wall_ms=solve_ms,
                            solve_ms=solve_ms,
                            reason=str(exc),
                        )
                    )
                    print(
                        json.dumps({"event": "step_unsupported", "stepId": step.step_id}),
                        flush=True,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    solve_ms = (time.perf_counter() - solve_started) * 1000.0
                    blocked_by = step.step_id
                    blocked_kind = "failed"
                    outcomes.append(
                        StepOutcome(
                            step_id=step.step_id,
                            status="failed",
                            wall_ms=solve_ms,
                            solve_ms=solve_ms,
                            reason=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    print(
                        json.dumps(
                            {"event": "step_failed", "stepId": step.step_id, "reason": str(exc)}
                        ),
                        flush=True,
                    )
                    continue
                solve_ms = (time.perf_counter() - solve_started) * 1000.0

            total_solve_ms += solve_ms

            try:
                frame: Frame | None = None
                frame_ms = 0.0
                if job.emit_frames:
                    t0 = time.perf_counter()
                    frame = self.extract_frame(state, job, out_dir, step.step_id)
                    frame_ms = (time.perf_counter() - t0) * 1000.0

                labels: list[str] | None = None
                probe_ms = 0.0
                if probe_pts:
                    t0 = time.perf_counter()
                    labels = self.probe_points(state, probe_pts)
                    probe_ms = (time.perf_counter() - t0) * 1000.0

                section_data: dict[str, list[float | None]] | None = None
                section_ms = 0.0
                if section_spec is not None:
                    t0 = time.perf_counter()
                    section_data = self.section_heights(state, section_spec)
                    section_ms = (time.perf_counter() - t0) * 1000.0

                ckpt: str | None = None
                checkpoint_ms = 0.0
                if job.emit_checkpoints:
                    t0 = time.perf_counter()
                    ckpt = self.save_checkpoint(state, out_dir, step.step_id)
                    checkpoint_ms = (time.perf_counter() - t0) * 1000.0

                outcome = StepOutcome(
                    step_id=step.step_id,
                    status="ok",
                    wall_ms=solve_ms,
                    solve_ms=solve_ms,
                    frame_ms=frame_ms,
                    probe_ms=probe_ms,
                    section_ms=section_ms,
                    checkpoint_ms=checkpoint_ms,
                    frame=frame,
                    checkpoint=ckpt,
                    metrics={
                        **step_notes,
                        "solveMs": round(solve_ms, 4),
                        "frameMs": round(frame_ms, 4),
                        "probeMs": round(probe_ms, 4),
                        "sectionMs": round(section_ms, 4),
                        "checkpointMs": round(checkpoint_ms, 4),
                        "probePoints": len(probe_pts),
                    },
                    probe=labels,
                    section=section_data,
                )
            except Exception as exc:  # noqa: BLE001
                outcome = StepOutcome(
                    step_id=step.step_id,
                    status="failed",
                    wall_ms=solve_ms,
                    solve_ms=solve_ms,
                    reason=f"{type(exc).__name__}: {exc}",
                )
                blocked_by = step.step_id
                blocked_kind = "failed"

            outcomes.append(outcome)
            print(
                json.dumps(
                    {"event": "step_done", "stepId": step.step_id, "status": outcome.status}
                ),
                flush=True,
            )

        info = self.info()
        return {
            "engine": _as_json(info),
            "outcomes": [_as_json(o) for o in outcomes],
            "run": {
                "solveMode": info.solve_mode,
                "initMs": round(init_ms, 4),
                "solveMs": round(total_solve_ms, 4),
                "jobAlgorithms": dict(job.algorithms),
                "registeredModels": self.models.registered_models(),
                "algorithms": self.models.describe_algorithms(),
            },
        }


def _as_json(payload: Any) -> Any:
    if is_dataclass(payload) and not isinstance(payload, type):
        return asdict(payload)
    if isinstance(payload, dict):
        return {k: _as_json(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_as_json(v) for v in payload]
    return payload


def run_adapter_cli(adapter: EngineAdapterBase[Any], argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Run {adapter.engine_id} engine adapter")
    parser.add_argument("job", nargs="?", type=Path)
    parser.add_argument("out_dir", nargs="?", type=Path)
    parser.add_argument("--describe", action="store_true", help="print EngineInfo JSON and exit")
    args = parser.parse_args(argv)

    if args.describe:
        info = _as_json(adapter.info())
        info["registeredModels"] = adapter.models.registered_models()
        info["algorithms"] = adapter.models.describe_algorithms()
        print(json.dumps(info, indent=2))
        return 0

    if args.job is None or args.out_dir is None:
        parser.error("job and out_dir are required unless --describe is passed")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    job = JobSpec.from_json(json.loads(args.job.read_text(encoding="utf-8")))
    try:
        result = adapter.run_job(job, args.out_dir)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        result = {
            "engine": _as_json(adapter.info()),
            "outcomes": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    write_json(args.out_dir / "result.json", result)
    return 0 if "error" not in result else 1
