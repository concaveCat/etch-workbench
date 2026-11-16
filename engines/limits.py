"""Cost estimate and domain guard.

Every engine declares ``limits`` (see :mod:`engines.registry`) and every step gets a
cheap estimate before it runs. When the estimate exceeds a limit the engine reports
``out_of_domain`` **and stops advancing that structure** -- a step that is skipped
cannot be silently followed by steps measured on a different geometry.

The two numbers that matter for this project's target (10 ms geometry steps):

* ``activePoints`` -- surface cells in the window, ``(width/dx)*(depth/dx)``;
* ``ratio`` -- displacement over grid spacing, ``max|rate|*duration/dx``. The design
  target is ``ratio <= 3`` for the accelerated path; beyond that the physics is the
  same but a one-shot update is no longer legal.
"""

from __future__ import annotations

from typing import Any

from engines.protocol import JobSpec, StepIR

# Defaults per engine id; a registration may override them.
DEFAULT_LIMITS: dict[str, dict[str, float]] = {
    "viennaps": {"max_active_points": 5.0e6, "max_ratio": 200.0},
    "spx": {"max_active_points": 1.0e7, "max_ratio": 2000.0},
}


def estimate(job: JobSpec, step: StepIR) -> dict[str, Any]:
    """Cheap cost estimate for one step; no engine call, no geometry traversal."""
    delta = float(job.domain.grid_delta) or 1.0
    active = (float(job.domain.width) / delta) * (float(job.domain.depth) / delta)
    rate = max((abs(float(value)) for value in step.rates.values()), default=0.0)
    displacement = rate * float(step.duration or 0.0)
    ratio = displacement / delta
    return {
        "activePoints": int(active),
        "displacementUm": round(displacement, 6),
        "ratio": round(ratio, 4),
        "gridDeltaUm": delta,
    }


def check(engine_id: str, job: JobSpec, step: StepIR,
          limits: dict[str, Any] | None = None) -> tuple[str | None, dict[str, Any]]:
    """Return ``(reason, estimate)``; ``reason`` is None when the step is in domain."""
    effective = {**DEFAULT_LIMITS.get(engine_id, {}), **(limits or {})}
    metrics = estimate(job, step)
    max_active = effective.get("max_active_points")
    if max_active is not None and metrics["activePoints"] > float(max_active):
        return (f"active points {metrics['activePoints']} exceed the {engine_id} limit "
                f"{int(float(max_active))}", metrics)
    max_ratio = effective.get("max_ratio")
    if max_ratio is not None and metrics["ratio"] > float(max_ratio):
        return (f"displacement {metrics['displacementUm']} um is {metrics['ratio']} grid "
                f"cells, above the {engine_id} limit {float(max_ratio)}", metrics)
    return None, metrics
