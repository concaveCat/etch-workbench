"""Scheduler: run one StepIR sequence on several engines and align the results.

Phase A keeps the scheduler deliberately small and explicit:

* one subprocess per engine, engines may run concurrently;
* every step is aligned by ``step_id``; a missing or failed step is recorded as
  such -- frames are never interpolated or silently substituted;
* no engine is retried on another engine. A step an engine cannot lower stays
  ``unsupported`` in ``compare.json``.

The output (``compare.json``) is the only thing the comparison UI reads.
"""

from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from engines.metrics import describe as describe_geometry
from engines.probe import build_lattice, occupancy_metrics
from engines.section import section_metrics
from engines.protocol import (Frame, JobSpec, ProbeSpec, SectionSpec, StepOutcome,
                              write_json)
from engines.registry import EngineRegistration, registrations

DEFAULT_TIMEOUT = 1800.0


@dataclass(frozen=True)
class EngineRun:
    engine_id: str
    out_dir: Path
    result: dict[str, Any]
    log: str
    wall_ms: float
    returncode: int


def _run_engine(reg: EngineRegistration, job_path: Path, out_dir: Path,
                timeout: float = DEFAULT_TIMEOUT) -> EngineRun:
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    completed = subprocess.run(
        reg.launch(job_path, out_dir), cwd=str(Path(job_path).parent),
        env={**__import__("os").environ, **reg.env},
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    wall_ms = (time.perf_counter() - started) * 1000.0
    result_path = out_dir / "result.json"
    if result_path.is_file():
        result = json.loads(result_path.read_text(encoding="utf-8"))
    else:
        result = {"engine": {"engine_id": reg.engine_id, "version": "unknown"},
                  "outcomes": [],
                  "error": (completed.stderr or completed.stdout or "no result.json").strip()[-2000:]}
    (out_dir / "runner.log").write_text(
        (completed.stdout or "") + ("\n--- stderr ---\n" + completed.stderr if completed.stderr else ""),
        encoding="utf-8")
    return EngineRun(reg.engine_id, out_dir, result, str(out_dir / "runner.log"),
                     wall_ms, completed.returncode)


def _outcomes(result: dict[str, Any]) -> dict[str, StepOutcome]:
    by_id: dict[str, StepOutcome] = {}
    for row in result.get("outcomes", []):
        raw_frame = row.get("frame")
        frame = None if not raw_frame else Frame(
            step_id=raw_frame["step_id"], kind=raw_frame["kind"], path=raw_frame["path"],
            width=int(raw_frame.get("width") or 0),
            height=int(raw_frame.get("height") or 0),
            camera=raw_frame.get("camera") or {},
            materials=raw_frame.get("materials") or [],
            bbox=raw_frame.get("bbox") or [],
        )
        outcome = StepOutcome(
            step_id=row["step_id"], status=row["status"], wall_ms=float(row["wall_ms"]),
            frame=frame, checkpoint=row.get("checkpoint"),
            metrics=row.get("metrics") or {}, reason=row.get("reason"),
            probe=row.get("probe"),
            section=row.get("section"),
        )
        if outcome.frame is not None:
            outcome.frame.validate()
        by_id[outcome.step_id] = outcome
    return by_id


def compare(job: JobSpec, engine_ids: list[str], out_root: Path,
            timeout: float = DEFAULT_TIMEOUT, max_probe_points: int = 3000) -> dict[str, Any]:
    """Run ``job`` on every engine and produce the aligned comparison record."""
    registry = registrations()
    unknown = [e for e in engine_ids if e not in registry]
    if unknown:
        raise SystemExit(f"unknown engine(s) {unknown}; known: {sorted(registry)}")

    job.validate()
    if job.probe is None and max_probe_points > 0:
        # The scheduler owns the lattice: every engine must answer the same points.
        measured = build_lattice(job.domain.width, job.domain.depth,
                                 job.initial.substrate_thickness,
                                 job.domain.height, max_probe_points)
        job = replace(job, probe=ProbeSpec(points=measured.points))
        probe_shape = list(measured.shape)
    else:
        probe_shape = None
    section_columns = None
    if job.section is None:
        # One section through the middle of the column, used for interface heights.
        stack = [job.initial.substrate, *[film.material for film in job.initial.films]]
        count = 33
        section_columns = [[job.domain.width * (i + 0.5) / count, job.domain.depth / 2]
                           for i in range(count)]
        job = replace(job, section=SectionSpec(
            columns=section_columns, materials=stack,
            z_min=-job.initial.substrate_thickness, z_max=job.domain.height))
    out_root = Path(out_root).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    job_path = out_root / "job.json"
    write_json(job_path, job.to_json())

    runs: dict[str, EngineRun] = {}
    with ThreadPoolExecutor(max_workers=len(engine_ids)) as pool:
        futures = {
            engine_id: pool.submit(_run_engine, registry[engine_id], job_path,
                                   out_root / "engines" / engine_id, timeout)
            for engine_id in engine_ids
        }
        for engine_id, future in futures.items():
            runs[engine_id] = future.result()

    per_engine = {engine_id: _outcomes(run.result) for engine_id, run in runs.items()}
    steps = []
    for step in job.steps:
        row: dict[str, Any] = {
            "stepId": step.step_id, "kind": step.kind, "mechanism": step.mechanism,
            "duration": step.duration, "rates": step.rates,
            "comparability": step.comparability, "engines": {},
        }
        for engine_id in engine_ids:
            outcome = per_engine[engine_id].get(step.step_id)
            if outcome is None:
                row["engines"][engine_id] = {"status": "missing", "wallMs": None,
                                             "frame": None, "reason": "engine produced no row"}
                continue
            frame_path = None
            if outcome.frame is not None:
                frame_path = out_root / "engines" / engine_id / outcome.frame.path
            row["engines"][engine_id] = {
                "status": outcome.status,
                "wallMs": outcome.wall_ms,
                "geometry": describe_geometry(frame_path) if frame_path else {"status": "none"},
                "frame": None if outcome.frame is None else {
                    "kind": outcome.frame.kind,
                    "path": str((out_root / "engines" / engine_id / outcome.frame.path)
                                .relative_to(out_root)),
                    "width": outcome.frame.width, "height": outcome.frame.height,
                    "camera": outcome.frame.camera,
                },
                "reason": outcome.reason,
                "metrics": outcome.metrics,
            }
        ok = [e for e in row["engines"].values() if e["status"] == "ok"]
        row["aligned"] = len(ok) == len(engine_ids)
        row["probe"] = _probe_row(job, per_engine, step.step_id, engine_ids)
        row["section"] = _section_row(job, per_engine, step.step_id, engine_ids)
        steps.append(row)

    payload = {
        "schemaVersion": 1,
        "caseId": job.case_id,
        "domain": {"width": job.domain.width, "depth": job.domain.depth,
                   "height": job.domain.height,
                   "origin": job.domain.origin_xy},
        "probeShape": probe_shape,
        "sectionColumns": section_columns or (job.section.columns if job.section else []),
        "view": job.render.view,
        "camera": job.render.camera,
        "sectionZ": ([job.section.z_min, job.section.z_max] if job.section else None),
        "probeMethod": {"viennaps": "height_column", "spx": "meas_probe"},
        "probePoints": len(job.probe.points) if job.probe else 0,
        "jobDigest": _digest(job),
        "engines": {engine_id: {"version": run.result.get("engine", {}).get("version"),
                                "processWallMs": run.wall_ms,
                                "returncode": run.returncode,
                                "error": run.result.get("error")}
                    for engine_id, run in runs.items()},
        "steps": steps,
        "summary": {
            "engineCount": len(engine_ids),
            "stepCount": len(job.steps),
            "fullyAlignedSteps": sum(1 for row in steps if row["aligned"]),
            "totalWallMs": {engine_id: run.wall_ms for engine_id, run in runs.items()},
        },
    }
    write_json(out_root / "compare.json", payload)
    return payload


def _section_row(job: JobSpec, per_engine: dict[str, dict[str, StepOutcome]],
                 step_id: str, engine_ids: list[str]) -> dict[str, Any]:
    if job.section is None:
        return {"status": "disabled"}
    outcomes = {engine_id: per_engine[engine_id].get(step_id) for engine_id in engine_ids}
    missing = [engine_id for engine_id, outcome in outcomes.items()
               if outcome is None or outcome.section is None]
    if missing:
        return {"status": "incomplete", "reason": f"no section result from {missing}"}
    if len(engine_ids) != 2:
        return {"status": "single_engine"}
    left, right = engine_ids
    return section_metrics(job.section.columns, job.section.materials,
                           outcomes[left].section, outcomes[right].section,
                           left_name=left, right_name=right)


def _probe_row(job: JobSpec, per_engine: dict[str, dict[str, StepOutcome]],
               step_id: str, engine_ids: list[str]) -> dict[str, Any]:
    """Cross-engine occupancy metrics for one step, or the reason there are none."""
    if job.probe is None:
        return {"status": "disabled", "reason": "no probe lattice in the job"}
    labels = {engine_id: per_engine[engine_id].get(step_id) for engine_id in engine_ids}
    missing = [engine_id for engine_id, outcome in labels.items()
               if outcome is None or outcome.probe is None]
    if missing:
        return {"status": "incomplete",
                "reason": f"no probe result from {missing}"}
    if len(engine_ids) != 2:
        return {"status": "single_engine", "reason": "occupancy needs two engines"}
    left, right = engine_ids
    return occupancy_metrics(job.probe.points, labels[left].probe, labels[right].probe,
                             left_name=left, right_name=right)


def _digest(job: JobSpec) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(job.to_json(), sort_keys=True).encode("utf-8")).hexdigest()[:16]
