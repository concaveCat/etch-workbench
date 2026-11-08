"""ViennaPS as an ordinary engine plugin.

This plugin is intentionally the same shape as a custom engine: it imports its
own native package, constructs an ``EngineProvider``, and exposes the same
``EngineCapabilities`` / ``EngineLease`` / ``StateBackend`` contracts.  The
worker no longer special-cases ViennaPS.
"""

from __future__ import annotations

import viennaps as ps

from process_models.engine_plugins.registry import EngineContext
from process_models.v2.engine import (
    EngineCapabilities,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.engines.viennaps import ViennaPSMaterialRegistry
from process_models.v2.engines.viennaps_cellset import ViennaPSCellSetBackend
from process_models.v2.state_backend import HandleStateBackend

__all__ = ["PLUGIN", "ViennaPSEnginePlugin"]

_CPU_KERNELS = ("CPU_DISK", "CPU_TRIANGLE")
_GPU_KERNELS = ("GPU_DISK", "GPU_TRIANGLE")
_CAPABILITIES = (
    "geometry.levelset",
    "flux.analytic",
    "flux.raytrace",
    "surface.coverage",
    "field.cellset",
    "io.native_state",
)


class ViennaPSEnginePlugin:
    """Construct CPU/GPU ViennaPS providers for one run."""

    engine_id = "viennaps"
    version = "1.0.0"

    def create_provider(self, context: EngineContext) -> StaticEngineProvider:
        fingerprint = context.fingerprint or "viennaps-plugin"
        state_backend = HandleStateBackend(
            ps,
            id="viennaps",
            version=str(ps.__version__),
            material_registry=ViennaPSMaterialRegistry(ps),
        )
        cellset_backend = ViennaPSCellSetBackend(ps)
        engines = [
            StaticEngine(
                capabilities=EngineCapabilities(
                    engine_id=self.engine_id,
                    version=str(ps.__version__),
                    kind="native",
                    backends=("cpu",),
                    capabilities=_CAPABILITIES,
                    kernels=_CPU_KERNELS,
                ),
                handle=ps,
                fingerprint=fingerprint,
                state_backend=state_backend,
                cellset_backend=cellset_backend,
            )
        ]
        selected = context.selected_device
        if isinstance(selected, dict) and selected.get("uuid"):
            engines.append(
                StaticEngine(
                    capabilities=EngineCapabilities(
                        engine_id=self.engine_id,
                        version=str(ps.__version__),
                        kind="native",
                        backends=("gpu",),
                        capabilities=_CAPABILITIES,
                        kernels=_GPU_KERNELS,
                        devices=(str(selected["uuid"]),),
                    ),
                    handle=ps,
                    fingerprint=fingerprint,
                    state_backend=state_backend,
                    cellset_backend=cellset_backend,
                )
            )
        return StaticEngineProvider(engines)


PLUGIN = ViennaPSEnginePlugin()
