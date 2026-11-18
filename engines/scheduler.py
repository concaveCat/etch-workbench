"""Multi-engine scheduler: run one StepIR flow on N engines, align & benchmark.

Supports 1, 2, or N engines in a single run:

* Designates a ``reference`` engine (explicit, or first engine with ``role == 'reference'``
  such as ``spx``, or the last engine in a 2-engine run for backward compatibility);
* Computes vs-reference occupancy and cross-section accuracy for **every** candidate
  engine in ``steps[i].comparisons["<candidate>|<reference>"]``, while keeping
  ``steps[i].probe`` and ``steps[i].section`` populated for the primary pair;
* Strictly separates pure solver speed (``totalSolveMs`` / ``speedupVsReference``)
  from rendering, probing, and subprocess startup wall time;
* Supports ``parallel=False`` sequential execution so CPU/GPU benchmarks do not
  suffer cross-engine resource contention.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from engines.comparability import grade as comparability_grade
from engines.limits import estimate as step_estimate
from engines.limits import window_verdict
from engines.metrics import describe as describe_geometry
from engines.probe import build_lattice, occupancy_metrics, stack_layer_midplanes
from engines.protocol import (
    Frame,
    JobSpec,
    ProbeSpec,
    SectionSpec,
    StepOutcome,
    flow_materials,
    write_json,
)
from engines.registry import EngineRegistration, registrations
from engines.section import section_metrics

DEFAULT_TIMEOUT = 1800.0


@dataclass(frozen=True)
class EngineRun:
    engine_id: str
    out_dir: Path
    result: dict[str, Any]
    log: str
    wall_ms: float
    returncode: int


def _run_engine(
    reg: EngineRegistration,
    job_path: Path,
    out_dir: Path,
    timeout: float = DEFAULT_TIMEOUT,
) -> EngineRun:
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    completed = subprocess.run(
        reg.launch(job_path, out_dir),
        cwd=str(Path(job_path).parent),
        env={**os.environ, **reg.env},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        preexec_fn=_address_space_guard(reg),
    )
    wall_ms = (time.perf_counter() - started) * 1000.0
    result_path = out_dir / "result.json"
    if result_path.is_file():
        result = json.loads(result_path.read_text(encoding="utf-8"))
    else:
        result = {
            "engine": {"engine_id": reg.engine_id, "version": "unknown"},
            "outcomes": [],
            "error": (completed.stderr or completed.stdout or "no result.json").strip()[-2000:],
        }
    (out_dir / "runner.log").write_text(
        (completed.stdout or "")
        + ("\n--- stderr ---\n" + completed.stderr if completed.stderr else ""),
        encoding="utf-8",
    )
    return EngineRun(
        reg.engine_id,
        out_dir,
        result,
        str(out_dir / "runner.log"),
        wall_ms,
        completed.returncode,
    )


def _outcomes(result: dict[str, Any]) -> dict[str, StepOutcome]:
    by_id: dict[str, StepOutcome] = {}
    for row in result.get("outcomes", []):
        raw_frame = row.get("frame")
        frame = (
            None
            if not raw_frame
            else Frame(
                step_id=raw_frame["step_id"],
                kind=raw_frame["kind"],
                path=raw_frame["path"],
                width=int(raw_frame.get("width") or 0),
                height=int(raw_frame.get("height") or 0),
                camera=raw_frame.get("camera") or {},
                materials=raw_frame.get("materials") or [],
                bbox=raw_frame.get("bbox") or [],
            )
        )
        metrics = row.get("metrics") or {}
        solve_raw = row.get("solve_ms")
        frame_ms = float(
            row.get("frame_ms")
            or metrics.get("frameMs")
            or metrics.get("frame_ms")
            or 0.0
        )
        probe_ms = float(
            row.get("probe_ms")
            or metrics.get("probeMs")
            or metrics.get("probe_ms")
            or 0.0
        )
        section_ms = float(
            row.get("section_ms")
            or row.get("sectionMs")
            or metrics.get("sectionMs")
            or metrics.get("section_ms")
            or 0.0
        )
        checkpoint_ms = float(
            row.get("checkpoint_ms")
            or metrics.get("checkpointMs")
            or 0.0
        )
        outcome = StepOutcome(
            step_id=row["step_id"],
            status=row["status"],
            wall_ms=float(row["wall_ms"]),
            solve_ms=(float(solve_raw) if solve_raw is not None else None),
            frame_ms=frame_ms,
            probe_ms=probe_ms,
            section_ms=section_ms,
            checkpoint_ms=checkpoint_ms,
            frame=frame,
            checkpoint=row.get("checkpoint"),
            metrics=metrics,
            reason=row.get("reason"),
            probe=row.get("probe"),
            section=row.get("section"),
        )
        if outcome.frame is not None:
            outcome.frame.validate()
        by_id[outcome.step_id] = outcome
    return by_id


def _address_space_guard(reg: EngineRegistration):
    """Cap a child engine's address space when ``max_mem_mib`` is configured."""
    default_mib = reg.limits.get("max_mem_mib")
    if reg.engine_id == "spx" and default_mib is None:
        default_mib = 6144.0
    if default_mib is None:
        return None
    env_override = os.environ.get(f"VIENNAPS_WORKBENCH_{reg.engine_id.upper()}_MEM_MIB")
    if reg.engine_id == "spx" and env_override is None:
        env_override = os.environ.get("VIENNAPS_WORKBENCH_SPX_MEM_MIB")
    limit_mib = int(float(env_override)) if env_override else int(float(default_mib))
    limit = limit_mib * 1024 * 1024

    def _apply() -> None:
        import resource

        try:
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (ValueError, OSError):
            pass

    return _apply


def _select_reference(
    engine_ids: list[str],
    registry: dict[str, EngineRegistration],
    explicit_reference: str | None,
) -> str:
    if explicit_reference:
        if explicit_reference not in engine_ids:
            raise SystemExit(
                f"reference engine {explicit_reference!r} must be included in engines {engine_ids}"
            )
        return explicit_reference
    for eid in engine_ids:
        if registry[eid].role == "reference":
            return eid
    return engine_ids[-1]


def _candidate_pairs(engine_ids: list[str], reference: str) -> list[tuple[str, str]]:
    """Ordered ``(candidate, reference)`` pairs for N-engine evaluation."""
    if len(engine_ids) < 2:
        return []
    return [(eid, reference) for eid in engine_ids if eid != reference]


def compare(
    job: JobSpec,
    engine_ids: list[str],
    out_root: Path,
    timeout: float = DEFAULT_TIMEOUT,
    max_probe_points: int = 3000,
    *,
    reference: str | None = None,
    parallel: bool = True,
    emit_frames: bool | None = None,
    emit_checkpoints: bool | None = None,
    enable_section: bool = True,
) -> dict[str, Any]:
    """Run ``job`` on ``engine_ids`` and produce the aligned multi-engine record."""
    registry = registrations()
    unknown = [e for e in engine_ids if e not in registry]
    if unknown:
        raise SystemExit(f"unknown engine(s) {unknown}; known: {sorted(registry)}")
    if not engine_ids:
        raise SystemExit("at least one engine must be specified")

    ref_engine = _select_reference(engine_ids, registry, reference)
    pairs = _candidate_pairs(engine_ids, ref_engine)

    if emit_frames is not None:
        job = replace(job, emit_frames=bool(emit_frames))
    if emit_checkpoints is not None:
        job = replace(job, emit_checkpoints=bool(emit_checkpoints))

    job.validate()
    for engine_id in engine_ids:
        reason, _ = window_verdict(engine_id, job, limits=registry[engine_id].limits)
        if reason is not None:
            raise SystemExit(f"{engine_id}: {reason}")

    if job.probe is None and max_probe_points > 0:
        measured = build_lattice(
            job.domain.width,
            job.domain.depth,
            job.initial.substrate_thickness,
            job.domain.height,
            max_probe_points,
            extra_z=stack_layer_midplanes(job),
        )
        job = replace(job, probe=ProbeSpec(points=measured.points))
        probe_shape = list(measured.shape)
    elif job.probe is not None:
        probe_shape = None
    else:
        probe_shape = None

    section_columns = None
    if job.section is None and enable_section:
        materials = flow_materials(job, include_masks=False)
        count = 33
        section_columns = [
            [job.domain.width * (i + 0.5) / count, job.domain.depth / 2]
            for i in range(count)
        ]
        job = replace(
            job,
            section=SectionSpec(
                columns=section_columns,
                materials=materials,
                z_min=-job.initial.substrate_thickness,
                z_max=job.domain.height,
            ),
        )

    out_root = Path(out_root).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    job_path = out_root / "job.json"
    write_json(job_path, job.to_json())

    runs: dict[str, EngineRun] = {}
    if parallel and len(engine_ids) > 1:
        with ThreadPoolExecutor(max_workers=len(engine_ids)) as pool:
            futures = {
                engine_id: pool.submit(
                    _run_engine,
                    registry[engine_id],
                    job_path,
                    out_root / "engines" / engine_id,
                    timeout,
                )
                for engine_id in engine_ids
            }
            for engine_id, future in futures.items():
                runs[engine_id] = future.result()
    else:
        for engine_id in engine_ids:
            runs[engine_id] = _run_engine(
                registry[engine_id],
                job_path,
                out_root / "engines" / engine_id,
                timeout,
            )

    per_engine = {engine_id: _outcomes(run.result) for engine_id, run in runs.items()}
    steps = []
    for step in job.steps:
        row: dict[str, Any] = {
            "stepId": step.step_id,
            "kind": step.kind,
            "mechanism": step.mechanism,
            "model": step.model,
            "duration": step.duration,
            "rates": step.rates,
            "comparability": comparability_grade(engine_ids, step),
            "declaredComparability": step.comparability,
            "estimate": step_estimate(job, step),
            "engines": {},
            "comparisons": {},
        }
        for engine_id in engine_ids:
            outcome = per_engine[engine_id].get(step.step_id)
            if outcome is None:
                row["engines"][engine_id] = {
                    "status": "missing",
                    "wallMs": None,
                    "solveMs": None,
                    "frameMs": None,
                    "probeMs": None,
                    "sectionMs": None,
                    "frame": None,
                    "reason": "engine produced no row",
                }
                continue
            frame_path = None
            if outcome.frame is not None:
                frame_path = out_root / "engines" / engine_id / outcome.frame.path
            row["engines"][engine_id] = {
                "status": outcome.status,
                "wallMs": outcome.wall_ms,
                "solveMs": outcome.solve_ms,
                "frameMs": outcome.frame_ms,
                "probeMs": outcome.probe_ms,
                "sectionMs": outcome.section_ms,
                "geometry": describe_geometry(frame_path) if frame_path else {"status": "none"},
                "frame": (
                    None
                    if outcome.frame is None
                    else {
                        "kind": outcome.frame.kind,
                        "path": str(
                            (out_root / "engines" / engine_id / outcome.frame.path).relative_to(
                                out_root
                            )
                        ),
                        "width": outcome.frame.width,
                        "height": outcome.frame.height,
                        "camera": outcome.frame.camera,
                    }
                ),
                "reason": outcome.reason,
                "metrics": outcome.metrics,
            }

        ok = [e for e in row["engines"].values() if e["status"] == "ok"]
        row["aligned"] = len(ok) == len(engine_ids)

        for cand, ref in pairs:
            pair_key = f"{cand}|{ref}"
            row["comparisons"][pair_key] = {
                "probe": _pair_probe_row(job, per_engine, step.step_id, cand, ref),
                "section": _pair_section_row(job, per_engine, step.step_id, cand, ref),
            }

        # Primary probe/section fields preserved for 2-engine & existing UI callers.
        if pairs:
            primary_key = f"{pairs[0][0]}|{pairs[0][1]}"
            row["probe"] = row["comparisons"][primary_key]["probe"]
            row["section"] = row["comparisons"][primary_key]["section"]
        else:
            row["probe"] = _single_probe_status(job)
            row["section"] = _single_section_status(job)

        steps.append(row)

    engine_meta: dict[str, Any] = {}
    total_solve_ms: dict[str, float | None] = {}
    total_frame_ms: dict[str, float] = {}
    total_probe_ms: dict[str, float] = {}
    total_section_ms: dict[str, float] = {}
    probe_methods: dict[str, str] = {}

    for engine_id, run in runs.items():
        eng_info = run.result.get("engine") or {}
        run_info = run.result.get("run") or {}
        outs = list(per_engine[engine_id].values())

        # Solve time: prefer explicit batch/total solveMs/runMs, else sum per-step solve_ms.
        raw_total_solve = run_info.get("solveMs")
        if raw_total_solve is None:
            raw_total_solve = run_info.get("runMs")
        if raw_total_solve is not None:
            solve_total = round(float(raw_total_solve), 3)
        elif outs and all(o.solve_ms is not None for o in outs):
            solve_total = round(sum(float(o.solve_ms or 0.0) for o in outs), 3)
        else:
            solve_total = None

        f_total = round(sum(o.frame_ms for o in outs), 3)
        p_total = round(sum(o.probe_ms for o in outs), 3)
        s_total = round(sum(o.section_ms for o in outs), 3)

        total_solve_ms[engine_id] = solve_total
        total_frame_ms[engine_id] = f_total
        total_probe_ms[engine_id] = p_total
        total_section_ms[engine_id] = s_total
        probe_methods[engine_id] = str(
            eng_info.get("probe_method") or registry[engine_id].probe_method
        )

        engine_meta[engine_id] = {
            "label": registry[engine_id].label,
            "role": registry[engine_id].role,
            "version": eng_info.get("version"),
            "solveMode": eng_info.get("solve_mode") or registry[engine_id].solve_mode,
            "probeMethod": probe_methods[engine_id],
            "solveWallMs": solve_total,
            "frameWallMs": f_total,
            "probeWallMs": p_total,
            "sectionWallMs": s_total,
            "processWallMs": round(run.wall_ms, 3),
            "returncode": run.returncode,
            "error": run.result.get("error"),
        }

    ref_solve = total_solve_ms.get(ref_engine)
    speedup_vs_ref: dict[str, float | None] = {}
    for engine_id, solve_val in total_solve_ms.items():
        if ref_solve and solve_val and solve_val > 0:
            speedup_vs_ref[engine_id] = round(ref_solve / solve_val, 3)
        else:
            speedup_vs_ref[engine_id] = None

    accuracy_summary: dict[str, Any] = {}
    for cand, ref in pairs:
        pair_key = f"{cand}|{ref}"
        agreements = [
            s["comparisons"][pair_key]["probe"]["agreement"]
            for s in steps
            if s["comparisons"][pair_key]["probe"].get("status") == "measured"
        ]
        active_agreements = [
            s["comparisons"][pair_key]["probe"]["activeAgreement"]
            for s in steps
            if s["comparisons"][pair_key]["probe"].get("status") == "measured"
        ]
        feature_agreements = [
            s["comparisons"][pair_key]["probe"]["featureAgreement"]
            for s in steps
            if s["comparisons"][pair_key]["probe"].get("status") == "measured"
        ]
        if agreements:
            sorted_agr = sorted(agreements)
            sorted_act = sorted(active_agreements)
            sorted_feat = sorted(feature_agreements)
            mid = len(sorted_agr) // 2
            accuracy_summary[pair_key] = {
                "meanAgreement": round(sum(agreements) / len(agreements), 4),
                "medianAgreement": round(sorted_agr[mid], 4),
                "minAgreement": round(min(agreements), 4),
                "meanActiveAgreement": round(sum(active_agreements) / len(active_agreements), 4),
                "medianActiveAgreement": round(sorted_act[mid], 4),
                "minActiveAgreement": round(min(active_agreements), 4),
                "meanFeatureAgreement": round(sum(feature_agreements) / len(feature_agreements), 4),
                "medianFeatureAgreement": round(sorted_feat[mid], 4),
                "minFeatureAgreement": round(min(feature_agreements), 4),
                "exactSteps": sum(1 for v in agreements if v >= 0.9999),
                "measuredSteps": len(agreements),
            }

    payload = {
        "schemaVersion": 1,
        "caseId": job.case_id,
        "referenceEngine": ref_engine,
        "executionMode": "parallel" if (parallel and len(engine_ids) > 1) else "sequential",
        "domain": {
            "width": job.domain.width,
            "depth": job.domain.depth,
            "height": job.domain.height,
            "origin": job.domain.origin_xy,
        },
        "probeShape": probe_shape,
        "sectionColumns": section_columns or (job.section.columns if job.section else []),
        "view": job.render.view,
        "camera": job.render.camera,
        "sectionZ": ([job.section.z_min, job.section.z_max] if job.section else None),
        "probeMethod": probe_methods,
        "probePoints": len(job.probe.points) if job.probe else 0,
        "jobDigest": _digest(job),
        "engines": engine_meta,
        "steps": steps,
        "summary": {
            "engineCount": len(engine_ids),
            "referenceEngine": ref_engine,
            "stepCount": len(job.steps),
            "fullyAlignedSteps": sum(1 for row in steps if row["aligned"]),
            "totalSolveMs": total_solve_ms,
            "speedupVsReference": speedup_vs_ref,
            "totalFrameMs": total_frame_ms,
            "totalProbeMs": total_probe_ms,
            "totalSectionMs": total_section_ms,
            "totalWallMs": {engine_id: round(run.wall_ms, 3) for engine_id, run in runs.items()},
            "accuracy": accuracy_summary,
        },
    }
    write_json(out_root / "compare.json", payload)
    return payload


def _single_section_status(job: JobSpec) -> dict[str, Any]:
    if job.section is None:
        return {"status": "disabled"}
    return {"status": "single_engine"}


def _single_probe_status(job: JobSpec) -> dict[str, Any]:
    if job.probe is None:
        return {"status": "disabled", "reason": "no probe lattice in the job"}
    return {"status": "single_engine", "reason": "occupancy needs at least two engines"}


def _pair_section_row(
    job: JobSpec,
    per_engine: dict[str, dict[str, StepOutcome]],
    step_id: str,
    left: str,
    right: str,
) -> dict[str, Any]:
    if job.section is None:
        return {"status": "disabled"}
    outcomes = {eid: per_engine[eid].get(step_id) for eid in (left, right)}
    missing = [eid for eid, outcome in outcomes.items() if outcome is None or outcome.section is None]
    if missing:
        return {"status": "incomplete", "reason": f"no section result from {missing}"}
    return section_metrics(
        job.section.columns,
        job.section.materials,
        outcomes[left].section,  # type: ignore[arg-type]
        outcomes[right].section,  # type: ignore[arg-type]
        left_name=left,
        right_name=right,
    )


def _pair_probe_row(
    job: JobSpec,
    per_engine: dict[str, dict[str, StepOutcome]],
    step_id: str,
    left: str,
    right: str,
) -> dict[str, Any]:
    if job.probe is None:
        return {"status": "disabled", "reason": "no probe lattice in the job"}
    outcomes = {eid: per_engine[eid].get(step_id) for eid in (left, right)}
    missing = [eid for eid, outcome in outcomes.items() if outcome is None or outcome.probe is None]
    if missing:
        return {"status": "incomplete", "reason": f"no probe result from {missing}"}
    return occupancy_metrics(
        job.probe.points,
        outcomes[left].probe,  # type: ignore[arg-type]
        outcomes[right].probe,  # type: ignore[arg-type]
        left_name=left,
        right_name=right,
    )


def _digest(job: JobSpec) -> str:
    return hashlib.sha256(
        json.dumps(job.to_json(), sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
