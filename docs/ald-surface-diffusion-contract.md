# ALD surface-diffusion contract (`SurfaceDiffusionParameters`)

Backend/API contract for the optional `steps[].surfaceDiffusion` object on
`model: "specialized"`, `nativeModel: "ald"`.

## 1. What the installed binding actually does

The audit row said "level-set smoothing model; no evidenced insertion point".
The bounded probe [`worker/probe_surface_diffusion_native.py`](../worker/probe_surface_diffusion_native.py)
and the ViennaPS 4.6.2 sources show something narrower:

| Source | Fact |
| --- | --- |
| `include/viennaps/process/psProcess.hpp` | `Process.setParameters(const SurfaceDiffusionParameters &)` stores the struct in the process context (bound in both `viennaps.d2.Process` and `viennaps.d3.Process`) |
| `include/viennaps/process/psProcessContext.hpp` | `flags.hasSurfaceDiffusion` is set only when the process model's surface model returns `getDiffusionCoefficients()` |
| `include/viennaps/models/psSingleParticleALD.hpp` | In 4.6.2 the only implementation returns `{"Coverage": coverageDiffusionCoefficient}` and only when that coefficient is `> 0` |
| `include/viennaps/process/psALPStrategy.hpp` | The ALP strategy builds the stencil from the current disk mesh. Each pulse substep calls the diffusion helper first on the flux data — which has no scalar named `Coverage`, so the call returns without changing anything — and then on the coverage data. The purge loop updates coverage from desorption and calls the helper only on the coverage data. Only `Coverage` is ever diffused |
| `include/viennaps/process/psSurfaceDiffusion.hpp` | The diffusion is a sequence of explicit-Euler Laplacian substeps on the scalar field, with a kNN/radius stencil weighted by distance and normal agreement. The native loop repeats steps of `dt = min(stabilityFactor·h²/(4D), timeStep)` until the interval is covered; the final substep can overshoot |

Consequences that shape the API:

- **It is not a level-set smoother.** The surface geometry only changes because
  the smoothed `Coverage` feeds `calculateVelocities()`; there is no native
  geometric/level-set diffusion call to expose.
- **It is inert on every other process** (including isotropic, single-particle,
  multi-particle, directional and the plasma models), because those surface
  models return no diffusion coefficients.
- **It is inert on ALD when `coverageDiffusionCoefficient <= 0`**, because the
  ALP flag is never set. The worker and schema therefore reject that
  combination when the object is enabled instead of exposing a dead knob. A
  disabled object on ALD is still limited to the default fields and does not
  call the native setter.
- **Placement is ALD-only even when disabled.** The object is rejected on every
  other specialized model regardless of `enabled`, because no other 4.6.2
  process has a native surface-diffusion flag.

## 2. Schema

```jsonc
{
  "model": "specialized",
  "nativeModel": "ald",
  "nativeParameters": {
    "coverageDiffusionCoefficient": 5.0,   // must be > 0 to enable the object
    // ...
  },
  "surfaceDiffusion": {                    // optional; omit for old behaviour
    "enabled": true,
    "radius": 0.1,
    "sigmaNormal": 0.2,
    "stabilityFactor": 0.5,
    "kNeighbors": 16,
    "normalCutoff": 0.25,
    "normalizeByLocalScale": true,
    "symmetrizeWeights": true
  }
}
```

| Key | Native member | Default | Bounds | Unit / meaning |
| --- | --- | --- | --- | --- |
| `enabled` | — | `true` | boolean | ALD-only placement applies either way; `false` is accepted only for an all-default object and makes the worker skip the setter (no positive coefficient required) |
| `radius` | `radius` | `0.0` | `0 … 2` and `≤ 8 × gridDelta` | µm; `0` selects `kNeighbors` mode. Points farther than this are not neighbours. The extra grid cap keeps the sparse stencil local |
| `kNeighbors` | `kNeighbors` | `16` | `1 … 64` | count; used when `radius == 0` |
| `normalCutoff` | `normalCutoff` | `0.25` | `-1 … 1` | cosine; neighbours with `n_i · n_j <= cutoff` are dropped |
| `sigmaNormal` | `sigmaNormal` | `0.35` | `> 0 … 10` | normal-agreement width in `exp(-(1-(n_i·n_j)^2)/sigmaNormal²)`; must be nonzero |
| `stabilityFactor` | `stabilityFactor` | `1.0` | `(0, 1]` | CFL factor; native `dt = min(stabilityFactor·h²/(4D), timeStep)` |
| `normalizeByLocalScale` | `normalizeByLocalScale` | `true` | boolean | divides the distance weight by `h²` |
| `symmetrizeWeights` | `symmetrizeWeights` | `true` | boolean | averages `w(i,j)` and `w(j,i)` and symmetrizes the stencil |

Defaults are read from the installed native struct in
`tests/test_ald_surface_diffusion.py`; they equal the `SurfaceDiffusionParameters()`
defaults.

## 3. Guarantees

- **Omitted ⇒ unchanged.** `surfaceDiffusion` is `None` by default, excluded
  from the canonical request JSON, and the base ALD request hashes stay pinned
  (`3c7c81…` 2D, `7d5ca4…` 3D).
- **Explicit defaults are a behaviour no-op.** With a positive coefficient,
  sending all native defaults is bit-identical to omitting the object; only the
  request hash changes because the user opted in.
- **Real native path.** The worker constructs `ps.SurfaceDiffusionParameters`,
  sets all seven members, calls `process.setParameters(...)` (the installed
  overload), reads every member back, and records the result under
  `actualExecution[].actualParameters.nativeModel.surfaceDiffusion`
  (`requested`, `effective`, `nativeMetaData`, `nativeMetaDataString`,
  `coverageDiffusionCoefficient`, `nativeInsertionPoint`, `appliesTo`).
- **Deterministic.** The stencil is built from the current deterministic disk
  mesh; repeated runs with the same seed and parameters hash identical.
- **Materials preserved.** Coverage diffusion redistributes a scalar; it does
  not add/remove level sets or change `MaterialMap` identities.
- **Rejected as inert or unsafe:** non-ALD models even when `enabled` is
  `false`; `coverageDiffusionCoefficient <= 0` when enabled; unknown keys;
  non-finite values; out-of-range values; and a disabled object that still
  overrides a field.

## 4. Probe evidence (bounded, 2D and 3D)

`worker/probe_surface_diffusion_native.py` ran as an isolated subprocess with a
600 s timeout and no workbench state. Trench domain, ALD, 400 rays, fixed seed:

| Case | 2D | 3D |
| --- | --- | --- |
| `coeff=0`, object omitted vs explicit defaults | same height hash | same height hash |
| `coeff=2`, object omitted vs explicit defaults | same height hash | same height hash |
| `coeff=2`, `radius=0.1` vs defaults | different hash, `max Δh ≈ 6.3e-6 µm` | different hash, `max Δh ≈ 3.0e-5 µm` |
| `coeff=2`, `radius=0.3` | different hash | different hash |
| `coeff=2`, `sigmaNormal=0.05` | different hash | different hash |
| `coeff=2`, `kNeighbors=4` | different hash | different hash |
| `coeff=2`, `stabilityFactor=0.25` | different hash | different hash |
| `coeff=2`, `symmetrizeWeights=false` | different hash | different hash |
| repeat `radius=0.1` | identical hash | identical hash |
| material map after every case | unchanged | unchanged |
| isotropic process with/without the object | identical hash | identical hash |

The changed cases differ in the sub-1e-5 µm range because diffusion
redistributes coverage rather than adding volume; the 3D trench shows the
largest signal at `kNeighbors=4` and `radius=0.1`.

## 5. Limitations

- The object cannot smooth a level-set surface in 4.6.2; no public API exists
  for that. Do not describe this field as geometric smoothing in UI copy.
- Its observable effect depends on a non-uniform ALD coverage field. On a flat,
  uniformly illuminated plane with a low coefficient the surface change can be
  below the grid resolution; that is a physical property of the native model,
  not a reason to widen ranges.
- `radius` is in project length units (µm), the same coordinate system as the
  disk mesh. It is capped at `min(2 µm, 8 × gridDelta)` because the native
  radius query materializes one sparse row per surface point; a domain-wide
  radius would be quadratic in point count and can exhaust worker memory before
  the artifact budget runs. Use `radius = 0` with `kNeighbors` for a
  point-count-bounded stencil.
- GPU coverage is not claimed; the ALD path stays CPU_DISK.
