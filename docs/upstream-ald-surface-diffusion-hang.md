# Upstream issue draft — 4.7.0 ALD + SurfaceDiffusionParameters hangs

**Status: local draft only. Do not publish without user approval.**

## Title

ViennaPS 4.7.0: `SingleParticleALD` + non-default `SurfaceDiffusionParameters`
never returns from `Process.apply()` (worked in 4.6.2)

## Versions

- ViennaPS 4.7.0, ViennaLS 5.10.0 (official PyPI wheels,
  `manylinux_2_27_x86_64.manylinux_2_28_x86_64`, CPython 3.12)
- Also tested: ViennaPS 4.6.2 + ViennaLS 5.8.5 (same script returns)
- Python 3.12.14, Linux x86_64
- Engine: `CPU_DISK`, `setNumThreads(1)`, no GPU

## Minimal reproducer

`scripts/migration-probes/probe_ald_hang.py` in the Workbench repository. It is
standalone (imports only `viennaps`) and does this:

1. 2D domain, `gridDelta=0.2`, `xExtent=4`, reflective boundary.
2. Si / SiO₂ 0.4 µm / Si₃N₄ 1.2 µm stack, 0.6 µm trench through the nitride
   (`GeometryFactory.makeBoxStencil` + `RELATIVE_COMPLEMENT`), duplicate the top
   level set as Al₂O₃.
3. `SingleParticleALDParams` with `coverageDiffusionCoefficient=5.0`,
   `growthPerCycle=0.05`, `numCycles=2`, `pulseTime=0.02`.
4. Add `AtomicLayerProcessParameters`, pinned `RayTracingParameters`
   (`raysPerPoint=64`, `rngSeed=271828`, `useRandomSeeds=False`,
   `maxBoundaryHits=80`, `maxReflections=3`, `normalizationType=SOURCE`,
   `smoothingNeighbors=1`) and `SurfaceDiffusionParameters` with
   `radius=0.1`, `sigmaNormal=0.2`, `kNeighbors=8`, `stabilityFactor=0.5`,
   `symmetrizeWeights=True`, `normalizeByLocalScale=True`,
   `normalCutoff=0.25`.
5. `ps.Process(domain, model, 0.02).setFluxEngineType(CPU_DISK)`;
   `setParameters` for advection (`timeStepRatio=0.25`), atomic, rays,
   diffusion; `apply()`.

Run with a wall-clock guard:

```bash
timeout 30 python probe_ald_hang.py
```

## Expected

```
viennaps 4.7.0 / viennals 5.10.0: applying
returned after ~0.1s
nodes 86
```

## Actual

```
viennaps 4.7.0 / viennals 5.10.0: applying
<no further output>
```

`Process.apply()` does not return; the process is still running after 10 s
(3/3 attempts) and was killed by `timeout`. The 4.6.2 run of the same script
prints `returned after 0.010s` and `nodes 86`.

The ALD progress output reaches the end of the last cycle (`Pulse time:
0.0200 / 0.0200 s`) before the hang. Default `SurfaceDiffusionParameters`
values, or a zero `coverageDiffusionCoefficient`, return normally. Removing the
`RayTracingParameters` block also makes this case return, so the trigger is the
combination of the non-default diffusion stencil with the pinned ray setup.

## Workaround / current application policy

- Do not pass `SurfaceDiffusionParameters` on 4.7.0: the Workbench rejects an
  enabled `steps[].surfaceDiffusion` before starting a worker, and the worker
  builder repeats the same gate before any native setter for hand-edited
  input.
- Stay on ViennaPS 4.6.2 / ViennaLS 5.8.5 where the same request returns in
  ~0.01 s.
- Default diffusion parameters are a no-op and do not hang, but they also do
  not exercise the stencil.

## What would help

A fix or a documented constraint for `surfaceDiffusion.radius > 0` (neighbour
search) on the 4.7.0 CPU ray-tracing path. A bounded timeout or convergence
guard inside the stencil would turn the hang into a diagnosable error.
