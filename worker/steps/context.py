"""Shared mutable state for the worker step loop.

``RunContext`` bundles the values the per-model executors read and the lists
they append to. Native ``Process``/model handles are deliberately *not* fields
here: the loop keeps them local so the per-step GPU teardown still sees them go
out of scope.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any


def simulate_module() -> Any:
    """Return the worker module that owns the shared native helpers.

    ``worker/simulate.py`` is reached through three entry points, and each one
    leaves it in ``sys.modules`` under a different key:

    * ``worker.simulate`` -- tests and snapshot materialisers import it by path;
    * ``simulate`` -- fixtures and probes put ``worker/`` on ``sys.path`` and use
      the top-level spelling, which is a *separate* module object carrying its
      own module-level state (``MATERIAL_IDS``, registered native materials, ...);
    * ``__main__`` -- the bounded child executes the file as a script.

    Every already-loaded candidate is accepted only when its resolved
    ``__file__`` is this worker's ``simulate.py``; identity is decided on the
    resolved path rather than the basename, so an unrelated script that happens
    to be called ``simulate.py`` cannot win. Only when none matches do we import
    the canonical name; importing while another entry is live would execute the
    whole worker module a second time and duplicate its module-level state.
    """
    target = Path(__file__).resolve().parents[1] / "simulate.py"
    for name in ("worker.simulate", "simulate", "__main__"):
        module = sys.modules.get(name)
        if module is None:
            continue
        source = getattr(module, "__file__", None)
        if source and Path(source).resolve() == target:
            return module
    return import_module("worker.simulate")


@dataclass
class RunContext:
    project: dict[str, Any]
    ps: Any
    domain: Any
    run_dir: Path
    run_id: str
    config: dict[str, Any]
    actual_steps: list[dict[str, Any]]
    step_engines: dict[str, str]
    engine_preference: str
    run_seed: int | None
    selected_gpu_device: dict[str, Any] | None
    cuda_readback: dict[str, Any] | None
    gpu_sample: dict[str, Any] | None
    native_engine_fingerprint: str | None
    ps_version: str
    ls_version: str
    snapshots: list[dict[str, Any]]
    field_state: dict[str, Any]
    actual_execution: list[dict[str, Any]]
    intermediate_outputs: list[dict[str, Any]]
    #: Persistent v2 state used by the opt-in execution path. The legacy
    #: worker path leaves this None and rebuilds from the live Domain.
    v2_state: Any | None = None
    #: Run-scoped v2 registry/provider/backends. The worker creates it lazily
    #: on the first claimed step and reuses it for every subsequent step in
    #: the same run; it is not a second state store.
    v2_session: Any | None = None

    def event(self, data: dict[str, Any]) -> None:
        simulate_module().event(self.run_dir, data)

    def save_snapshot(self, domain: Any, **kwargs: Any) -> dict[str, Any]:
        return simulate_module().save_snapshot(self.ps, domain, self.run_dir, run_id=self.run_id, **kwargs)

    def save_native_state(self, domain: Any, step_id: str) -> dict[str, Any]:
        return simulate_module().save_native_state(
            self.ps,
            domain,
            self.run_dir,
            project=self.project,
            run_id=self.run_id,
            step_id=step_id,
            viennaps_version=self.ps_version,
            viennals_version=self.ls_version,
        )
