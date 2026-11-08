# Specialized capability release: `nvFactors`, 3D geometric trench, coverage tolerance

Installed engine: ViennaPS **4.6.2**, ViennaLS **5.8.5**. Probes were run in two
subprocesses per case (one `timeout`, no in-process crash containment): the
pristine `/root/.local` wheel and the multi-ion wheel under
`runtime-patched/current`. The patched wheel changes unrelated ViennaLS
narrow-band details (a duplicated 3D plane reports `z=0` versus `z=±0.1`), so
every claim below was checked against the production runtime as well.

## 1. `SelectiveEpitaxy.nvFactors`

### Native signature and semantics

The installed binding has three constructor overloads (both `viennaps.d2` and
`viennaps.d3`):

1. `SelectiveEpitaxy(rate111=0.5, rate100=1.0)`
2. `SelectiveEpitaxy(materialRates, rate111=0.5, rate100=1.0)`
3. `SelectiveEpitaxy(nvFactors: FixedSize(3), rate111=0.5, rate100=1.0)`

The third overload is a **separate native constructor**. It leaves
`materialRates_` empty, so rates must be applied with `setMaterialRate()` after
construction. `ps.SelectiveEpitaxy(nv, ...)` followed by `setMaterialRate()`
reproduces the second overload exactly when `nv` is the dimension default.

`psSelectiveEpitaxy.hpp` computes:

```
vel = max_i(|nvFactors[i] * normal[i]|)
vel = (vel - low) * (rate100 - rate111) / (1 - low) + rate111
return min(-vel * rate, 0.0)
low = 1/sqrt(2) in 2D, 1/sqrt(3) in 3D
```

Native defaults are dimension-dependent:

| dimension | default `nvFactors` |
| --- | --- |
| 2D | `[0.5, 1.0, 0.0]` (vertical is y) |
| 3D | `[0.5, 0.0, 1.0]` (vertical is z) |

There is no `nvFactors` getter. `getVelocityField()` returns an unregistered
`viennaps::impl::EpitaxyVelocityField` that pybind cannot convert, so the worker
records the requested/effective vector and states that native readback is not
available instead of inventing one.

### Probe results (flat Si + duplicated SiGe top, `rate111=0.4`, `rate100=1.0`, 1 s)

| dimension | factors | top growth (µm) |
| --- | --- | --- |
| 2D | omitted | 0.5 |
| 2D | `[0.5, 1, 0]` (default) | 0.5 (identical surface hash) |
| 2D | `[1, 1, 1]` | 0.5 on a flat surface |
| 2D | `[0.5, 0, 1]` | 0.0 (vertical factor 0 freezes the native velocity) |
| 3D | omitted | 0.5 |
| 3D | `[0.5, 0, 1]` (default) | 0.5 (identical surface hash) |
| 3D | `[0.5, 1, 0]` | 0.0 (2D default used in 3D freezes growth) |

### Exposed surface

`steps[].nativeParameters.nvFactors` is an optional strict three-vector of
finite numbers in `[0, 1]`. All-zero vectors are rejected because the native
velocity becomes zero and the run is a silent no-op. Omitting the key uses the
existing `materialRates` overload; the request hash and run artifacts are
unchanged. An explicit vector uses overload 3 plus `setMaterialRate()`, and the
worker records `nativeNvFactors` with requested/effective values, the overload,
the dimension default, and the reason readback is absent.

Range rationale: factors above 1 extrapolate `vel` beyond `rate100` and below 0
are reduced by the native `Abs`, so only `[0, 1]` keeps the documented
`[rate111, rate100]` interpolation.

## 2. `GeometricTrenchDeposition` in 3D

### What the 2026-10-30 audit actually found

The audit's 3D SIGSEGV command was:

```python
d = ps.Domain(0.1, 1.0, 1.0)
ps.MakePlane(d, 0.0, ps.Material.Si, False).apply()
ps.Process(d, ps.GeometricTrenchDeposition(0.4, 0.5, 0.05, 0.1, 0.1, 2.0, 1.0), 0.05).apply()
```

Re-probed on 2026-10-31, **the identical call also SIGSEGVs in 2D**
(`rc=139`). Bisecting the parameters shows the failure is the native
distribution reach, not the dimension:

* `TrenchDistribution::getBounds()` advertises `±depositionRate`.
* `getSignedDistance()` uses `radius = a*(1-|z|/trenchDepth)^n + b` (or
  `bottomMed` at the floor).
* Once `radius > depositionRate + 2*gridDelta`, `GeometricAdvect` can emit an
  **empty deposited level set**; further below the candidate window it aborts
  with SIGSEGV in `GOMP_parallel`.

Measured with `gridDelta=0.1`, `radius=a+b=0.4`, `trenchDepth=0.5`:

| `depositionRate` | result |
| --- | --- |
| 0.05, 0.08 | SIGSEGV |
| 0.10, 0.15, 0.19 | completes but leaves the top level set empty |
| 0.20 (`radius - 2*grid`) | completes, top level set populated |
| 0.40 (`radius`) | completes, top level set populated |

The earlier `rate + 3*grid` guard accepted the empty-layer cases, so it was
replaced by the conservative `depositionRate >= max(a+b, bottomMed)`. A
288-case stress matrix (2D/3D × grid 0.05/0.1/0.2 × plane/trench/fin ×
`n=1/2/10/100` × different `a/b/bottomMed`) produced no SIGSEGV and no empty
top level set at that boundary. Inputs between `radius - 2*grid` and `radius`
that the kernel happens to accept are deliberately rejected.

### Geometry matrix (guarded default parameters, workbench construction path)

| geometry | 2D | 3D |
| --- | --- | --- |
| plane | completes | completes |
| trench | completes | completes |
| hole | completes | completes |
| fin | completes | completes |
| stack (flat, mask/film layers) | completes | completes |
| GDS | rejected (no project-level z span) | rejected (same) |

A 3D trench Workbench run matches a direct native `ps.GeometricTrenchDeposition`
reference exactly (points/connectivity, coordinate tolerance `1e-6 µm`). The
worker never substitutes the 2D class: `type(model).__module__` is
`viennaps.d3` for 3D projects.

### Exposed surface and guards

`dimensions` is now `[2, 3]`. The shared rule
`backend.specialized_models.geometric_trench_issues()` is called by both
request validation and `build_specialized_model()`, so a direct worker input
cannot bypass it:

* `trenchWidth >= gridDelta` (the native kernel ignores this value, so a
  sub-grid width would only misdescribe the geometry);
* `trenchDepth >= max(initial |z|, gridDelta)`; the vertical extent uses
  `finHeight + maskHeight` for a fin (the mask sits above the fin),
  `max(depth, maskHeight)` for trench/hole (the mask and opening are on
  opposite sides of z=0), and for a stack the maximum of
  `baseHeight + layers` and `gdsMask.baseHeight + gdsMask.position[2] +
  gdsMask.height`. Taper changes lateral width only;
* `n` integer in `[1, 100]` (fractional `n` can make the native radius NaN);
* `a+b > 0` (the duplicated top level set must have a positive radius);
* `depositionRate >= max(a+b, bottomMed)`.

`geometry.gdsMask` exists only on `stack` in schema 7 (`StackV7.gdsMask`);
`GdsBooleanStepV7.gdsMask` is a step operand, and a GTD step cannot follow any
earlier enabled step. GDS geometry, continuation states, and non-first enabled
GTD steps keep their
422. The model remains `experimental` because no archived Stage 8/9
direct-reference row exists for it.

## 3. `CoverageParameters.tolerance`

### Native behavior

`CoverageParameters` defaults: `tolerance = 0.0`, `maxIterations = 4294967295`,
`initialized = false`. `CoverageManager::checkCoveragesConvergence()` stops when
every coverage delta metric is `<= tolerance`.

In `FluxProcessStrategy::coverageInitIterations()`:

* if `maxIterations` is still the unsigned default **and** `tolerance == 0.0`,
  the strategy replaces it with **10** iterations;
* otherwise the loop runs until convergence, so a negative/NaN tolerance never
  converges and would run up to `4294967295` iterations.

The worker never sets `maxIterations`.

### Exposed surface

`steps[].nativeParameters.coverageTolerance` is optional for `sf6o2`, `cf4o2`,
`hbro2` and `sf6c4f8`; range `[0, 1]`, finite. Omitting it keeps the historical
hard-coded `1e-3` and adds no worker metadata. An explicit value is applied to
the same native object and recorded under `nativeCoverageParameters` with the
`toMetaData()` readback. The worker re-checks the range before the native
setter, so a direct worker/replay input cannot send a negative tolerance to the
native loop.

Measured with a 2D SF6O2 stack, 32 rays, fixed seed:

| tolerance | `CoverageTolerance` readback | `CoverageMaxIterations` readback | surface hash |
| --- | --- | --- | --- |
| omitted (worker 1e-3) | `1e-3` | `4294967295` | equal to explicit 1e-3 |
| `0.0` | `0.0` | `10` | distinct |
| `1e-4` | `1e-4` | `4294967295` | equal to 1e-3 for this short case |
| `1e-3` | `1e-3` | `4294967295` | baseline |
| `0.1` | `0.1` | `4294967295` | equal to 1e-3 for this short case |

The surface difference is measurable between the 10-iteration fallback
(`0.0`) and a converged positive tolerance; above ~1e-4 the coverage field
converges to the same values for this short run, which is why the metadata
readback is the primary per-run evidence.

## 4. Hash and artifact stasis

* Omitting `nvFactors` keeps the existing `materialRates` constructor and the
  exact pinned request hashes.
* Omitting `coverageTolerance` keeps `tolerance=1e-3` and adds no
  `nativeCoverageParameters` metadata.
* Explicit values are part of `nativeParameters`, so they change the request
  hash; replay re-validates and reproduces them.

## 5. Limitations

* No Stage 8/9 archived direct-reference evidence exists for the two
  experimental models, so both still emit the experimental warning.
* `nvFactors` has no native getter; readback is requested/effective plus the
  native surface result, not an internal object dump.
* GDS geometry and continuation states remain unsupported for
  `GeometricTrenchDeposition`.
* Coverage tolerance effects on the final surface can be below the grid
  resolution for short runs; the native metadata readback and request hash
  changes are always observable.
