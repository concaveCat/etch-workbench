"""Minimal upstream repro: ALD + non-default SurfaceDiffusionParameters.

Standalone (no Workbench imports). On ViennaPS 4.7.0 / ViennaLS 5.10.0 this
reaches the end of the last ALD cycle and then spins forever inside
``Process.apply()``; the same script finishes in well under a second on
4.6.2 / 5.8.5.

Run with a wall-clock timeout, for example:
    timeout 30 .venv/bin/python scripts/migration-probes/probe_ald_hang.py
"""
from __future__ import annotations

import sys
import time

import viennaps as ps

ps.setDimension(2)
ps.Logger.setLogLevel(ps.LogLevel.ERROR)
ps.setNumThreads(1)

grid = 0.2
domain = ps.Domain(grid, 4.0, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
ps.MakePlane(domain, 0.0, ps.Material.Si).apply()
ps.MakePlane(domain, 0.4, ps.Material.SiO2, addToExisting=True).apply()
ps.MakePlane(domain, 1.6, ps.Material.Si3N4, addToExisting=True).apply()
# Pattern the top Si3N4 layer with a narrow trench so the coverage field has
# a gradient (same construction as the Workbench direct reference).
ps.Length.setUnit("um")
ps.Time.setUnit("s")
factory = ps.GeometryFactory(domain.getSetup(), "ald-hang-repro")
stencil = factory.makeBoxStencil([0.0, 1.6 - 1.2 - grid * 1e-3],
                                 0.6, 1.2 + 2 * grid * 1e-3, 0.0)
domain.applyBooleanOperation(stencil, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT, False)
domain.duplicateTopLevelSet(ps.Material.Al2O3)

parameters = ps.SingleParticleALDParams()
parameters.stickingProbability = 1.0
parameters.gasMeanFreePath = 1.0
parameters.growthPerCycle = 0.05
parameters.incomingFlux = 1.0
parameters.evaporationFlux = 0.0
parameters.s0 = 1.0
parameters.coverageDiffusionCoefficient = 5.0
model = ps.SingleParticleALD(parameters)

atomic = ps.AtomicLayerProcessParameters()
atomic.pulseTime = 0.02
atomic.purgePulseTime = 0.0
atomic.coverageTimeStep = 0.01
atomic.purgeTimeStep = 1.0
atomic.numCycles = 2

diffusion = ps.SurfaceDiffusionParameters()
diffusion.radius = 0.1
diffusion.sigmaNormal = 0.2
diffusion.kNeighbors = 8
diffusion.stabilityFactor = 0.5
diffusion.symmetrizeWeights = True
diffusion.normalizeByLocalScale = True
diffusion.normalCutoff = 0.25

process = ps.Process(domain, model, 0.02)
process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
advection = ps.AdvectionParameters()
advection.timeStepRatio = 0.25
process.setParameters(advection)
process.setParameters(atomic)
# The Workbench pins CPU_DISK ray tracing before the native extra objects.
rays = ps.RayTracingParameters()
rays.raysPerPoint = 64
rays.rngSeed = 271828
rays.useRandomSeeds = False
rays.maxBoundaryHits = 80
rays.maxReflections = 3
rays.minNodeDistanceFactor = 0.05
rays.normalizationType = ps.NormalizationType.SOURCE
rays.smoothingNeighbors = 1
process.setParameters(rays)
process.setParameters(diffusion)

print(f"viennaps {ps.__version__} / viennals {ps.ls.version}: applying", flush=True)
started = time.monotonic()
process.apply()
print(f"returned after {time.monotonic() - started:.3f}s", flush=True)
print("nodes", len(domain.getSurfaceMesh().getNodes()), flush=True)
