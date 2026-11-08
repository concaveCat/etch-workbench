# Oxidation advanced contract — `Si110` and `steps[].advancedOxidation`

This document pins the backend/API contract for batch B5 against the installed
engine (`viennaps` 4.6.2, `viennals` 5.8.5). It is deliberately narrower than
the native `Oxidation` class: only members that were proven to reach native code
and change a real 2D/3D surface are exposed.

## 1. Schema

`steps[].advancedOxidation` is an optional, strict object on
`model: "specialized"`, `nativeModel: "oxidation"`. Omitting it keeps every
historical request hash and artifact byte-identical. Unknown keys are rejected
by Pydantic (`extra="forbid"`), including every excluded native member listed in
section 4.

```json
{
  "id": "stage8_oxidation",
  "name": "硅热氧化",
  "enabled": true,
  "operation": "native",
  "model": "specialized",
  "nativeModel": "oxidation",
  "nativeParameters": {
    "oxidant": "Dry",
    "orientation": "Si110",
    "temperatureC": 1000,
    "pressureAtm": 1,
    "timeHours": 0.1,
    "timeStepHours": 0.03,
    "transferCoefficientUmHr": 0.1,
    "initialOxideThicknessUm": 0,
    "reactionActivationVolumeM3": 0,
    "diffusionActivationVolumeM3": 0,
    "maskMaterial": "",
    "siliconMaterial": "Si",
    "oxideMaterial": "SiO2"
  },
  "advancedOxidation": {
    "cflFactor": 0.05,
    "mechanicsTolerance": 0.05,
    "couplingTolerance": 0.1,
    "simplePressureRelaxation": 0.1
  }
}
```

All four fields are optional; each defaults to the installed native value.

| Field | Type | Default | Accepted range | Native setter | Unit |
| --- | --- | --- | --- | --- | --- |
| `cflFactor` | finite number | `0.499` | `0.01 … 0.499` | `Oxidation.setCFLFactor` | dimensionless Courant number |
| `mechanicsTolerance` | finite number | `0.01` | `0.01 … 0.2` | `Oxidation.setMechanicsTolerance` | native residual |
| `couplingTolerance` | finite number | `0.05` | `0.05 … 0.5` | `Oxidation.setCouplingTolerance` | native residual |
| `simplePressureRelaxation` | finite number | `0.5` | `0.01 … 1.0` | `Oxidation.setSimplePressureRelaxation` | dimensionless |

Cross-field rule: `couplingTolerance >= mechanicsTolerance`. The native model
already emits a warning when this is violated; the schema rejects it instead of
starting a run that cannot converge as requested.

Ranges are intentionally stricter than the native clamps where tightening was
never evidenced:

- `setCFLFactor` clamps to `[0.001, 0.499]` natively; the Workbench floor is
  `0.01` to keep bounded-child runs tractable.
- `mechanicsTolerance` below `0.01` and `couplingTolerance` below `0.05` are
  rejected. Controlled probes with `mechanicsTolerance=1e-4` and
  `couplingTolerance=1e-6` ended in
  `unable to find a converged CFL-limited step`.
- `pressureTolerance` / `stokesTolerance` are not independent knobs: loosening
  them requires a matching mechanics tolerance. They stay excluded rather than
  exposing a cross-coupled trap.

Non-finite numbers (`NaN`, `±Inf`), booleans, strings and JSON `null` are
rejected with a 422 before the worker starts.

## 2. Native insertion point and recording

The worker inserts the values only after the historical setters:

```
worker/specialized.py, model_id == "oxidation"
  … setSiliconMaterial / setOxideMaterial / setMaskMaterial …
  model.setCFLFactor(effective["cflFactor"])
  model.setMechanicsTolerance(effective["mechanicsTolerance"])
  model.setCouplingTolerance(effective["couplingTolerance"])
  model.setSimplePressureRelaxation(effective["simplePressureRelaxation"])
  model.estimatePlanarOxideThickness(initialOxideThicknessUm)   # native readback
```

There is no fallback: if the block is present it reaches these four setters, and
if any value is invalid the worker raises instead of dropping it.

`actualExecution[].actualParameters.nativeModel.advancedOxidation` records:

- `normalizedInput`: the normalized project object. The schema materialises
  every default, so submitting only `{"cflFactor": 0.05}` appears here with all
  four fields. The raw submitted key subset is not recoverable after
  normalization without changing the request hash or adding replay-only state;
  `inputSource` states this explicitly. This field was called `requested` in the
  first checkpoint and is renamed to avoid claiming raw-key fidelity.
- `defaults`: the installed native defaults;
- `effective`: every field after defaults and validation, i.e. the values passed
  to native setters;
- `nativeSetters`: the exact `Oxidation.set*` name per field;
- `nativeReadback.orientation`: the orientation passed to the native model;
- `nativeReadback.planarOxideThicknessUm`: the result of the bound
  `estimatePlanarOxideThickness`, which re-reads the orientation and initial
  oxide thickness natively;
- `nativeReadback.nativeGetters: "not exposed"` and `solverControls`: an honest
  statement that 4.6.2 has no getters for these four solver fields. The
  effective values are the validated values passed to the native setters; the
  behavioural evidence is the surface-hash probe below.
- `location`, `nativeClass`, `defaultsSource` and `schemaVersion`.

When the block is present, the same object is written under `oxidation` in the
step snapshot and in the supervisor's snapshot manifest. Deferred snapshot
materialisation preserves it via `worker/materialize_snapshot.py`. When the block
is omitted, neither the snapshot nor the manifest gains the key.

## 3. Orientation

`SiliconOrientation` is an `IntEnum` in the installed binding:

| Member | Native value | Dry 1000 °C / 0.5 h planar estimate |
| --- | --- | --- |
| `Si100` | 0 | 0.020625241855656007 µm |
| `Si110` | 1 | 0.027756723741431427 µm |
| `Si111` | 2 | 0.030837787588321106 µm |
| `PolySi` | 3 | 0.020625241855656007 µm (isotropic, <100> baseline) |

The Workbench now accepts all four names. Any other value (`Si101`, lowercase,
unknown) is a 422. `Si110` is not a fake enum token: it changes the Deal-Grove
readback and produces a distinct native surface hash in both dimensions.

## 4. Exclusions and why

The installed `Oxidation` binding has **45** `set*` methods. The Workbench now
reaches **17** of them (13 historical + 4 advanced). The remaining surface is
excluded for one of the reasons below.

### 4.1 Inert or unsafe solver controls

| Member | Probe result | Decision |
| --- | --- | --- |
| `setCouplingIterations` | explicit default `30` is byte-identical; `1` fails to converge (`pressure-concentration coupling failed`) | excluded |
| `setMechanicsIterations` | explicit default `200` is byte-identical; `1` fails (`deformation solve failed`) | excluded |
| `setPressureIterations` / `setStokesIterations` | explicit defaults `500` are byte-identical; pressure `1` fails | excluded |
| `setPressureTolerance` / `setStokesTolerance` | explicit defaults are byte-identical; loosening interacts with `mechanicsTolerance` | excluded |
| `setSimpleVelocityRelaxation` | no surface change above `1e-8` µm in the controlled 2D/3D probes | excluded as not observable |
| `setMaxGridPoints` | explicit default `5,000,000` is byte-identical; `1000`/`100` raise `Cartesian solve region exceeds maxGridPoints` | excluded — a cap, not a physics control |
| `setSolveBounds` / `clearSolveBounds` | raw Cartesian indices. `[2,2]…[28,28]` changes the surface, but `[8,8]…[20,20]` raises `Cartesian solve region is empty` | excluded as unsafe without a stable index contract |
| `setGpuMode` / `setGpuPreconditioner` | CPU is the default; GPU oxidation has no host evidence | excluded (CPU-only batch) |
| `saveSurfaceMesh` / `saveVolumeMesh` | Workbench writes the same meshes through `Domain`; these are output helpers, not parameters | excluded |
| `estimatePlanarOxideThickness` | read-only helper | not a knob; used as the native readback |

### 4.2 Mask mechanics

`setMaskMaterial` is already exposed as `nativeParameters.maskMaterial`. The
mask-mechanics setters (`setMaskParameters`, `setMaskTraction*`,
`setMaskContact*`, `setMaskUnilateralContact`, `setMaskSmootherOmega`,
`setMaskCoupling*`, `setMaskBendingBounds`) only activate when a mask level set is
present. A controlled 2D LOCOS stack (`Si` / thin `SiO2` / `Si3N4` with a mask
opening) produced an empty oxide level set after `Process.apply`, so none of
these fields could be proven to change a trustworthy returned surface. They stay
out until a verified LOCOS output exists; a non-mask run would be a silent
no-op, not a knob.

### 4.3 Legacy `OxidationParameters` names are not bound

The following names from the older ViennaLS oxidation interface do **not** exist
in the installed Python module (checked with `hasattr`): `OxidationParameters`,
`psOxidation`, `psProcessContext`, `oxidantFlux`, `reactionRate`,
`volumeExpansion`, `stabilityFactor`, `redoStabilityCheck`, `calculateError`,
`maxCellTravelDistance`, `useFluxSolver`, `fluxSolver`, `fluxMultiplier`,
`solverStabilityFactor`, `trapLayer`, `addDiffusionCellSet`,
`diffusionCellSet`, `maxGridPoints` (module-level), `timeStepRatio`,
`finiteVolume`, `fluxAtBoundary`. They are rejected as unknown keys. The actual
bound surface is `Oxidation.set*`, which this batch audited member by member.

`timeStepRatio` is an advection/solver parameter in this engine, not an
`Oxidation` setter. The oxidation time-step cap is already exposed as
`nativeParameters.timeStepHours` (`setTimeStep`), so no second time-step field is
introduced.

## 5. Probe evidence

Reproduce with:

```bash
cd /root/wb-engine-oxidation
.venv/bin/python worker/probe_oxidation_advanced_native.py
.venv/bin/python -m unittest tests.test_oxidation_advanced -v
```

Both native proof helpers pin `ps.FluxEngineType.CPU_DISK` before
`Process.apply()`, exactly like `worker/simulate.py`, so the evidence does not
depend on CUDA autodetection.

Controlled 2D configuration: `gridDelta=0.01` µm, `xExtent=1.0` µm, SiO₂
thickness 0.05 µm, 1000 °C dry, `timeHours=0.166`, `timeStepHours=0.03`.
Surface hashes are SHA-256 over rounded surface points plus `MaterialIds`.

| Case | Surface hash prefix | Same as default? |
| --- | --- | --- |
| defaults (`Si100`) | `7a4abfd437864a2e` | — |
| `cflFactor=0.05` | `9d60b177d29bbd52` | no |
| `mechanicsTolerance=0.05` | `42aba561f5f1b85b` | no |
| `simplePressureRelaxation=0.1` | `42aba561f5f1b85b` | no |
| `Si110` | `26034b152e0ed636` | no |
| `Si111` | `942ce7ceecfb68c7` | no |

`couplingTolerance=0.2` is inert at the 0.01 µm planar grid but changes the
surface at `gridDelta=0.02` (hash `dac673871ee66365`; both the default and the
advanced case have 102 points at that coarser grid). The test uses that
configuration.

3D configuration: `gridDelta=0.05` µm, `xExtent=yExtent=1.0` µm, SiO₂ thickness
0.15 µm, 1000 °C dry, `timeHours=0.1`, `timeStepHours=0.03`, 882 surface points.

| Orientation | 3D surface hash prefix |
| --- | --- |
| `Si100` | `01e7c670e7a44dc6` |
| `Si110` | `22674addf40c5ac7` |
| `Si111` | `e4b5f6b9400cdd6d` |

Repeating the same project produces the same hash. The 3D probe runs in a fresh
interpreter because the installed 4.6.2 proxy keeps dimension state
process-wide; switching 2D→3D in one process can return a stale single-surface
3D mesh.

Defaults evidence: passing all four defaults explicitly produces the same
surface hash as omitting the block in both 2D and 3D, despite the changed
request hash. This is the behavioural check that the missing native getters
cannot provide directly.

## 6. Hash and artifact stability

- `advancedOxidation` is `exclude_none=True` in `requested_input`, so an omitted
  block does not appear in historical request hashes. The 2D and 3D Stage-8
  oxidation base hashes are asserted in
  `tests/test_oxidation_advanced.py` (`a0beb0…5688` and `c9581e…b6f3b`).
- When the block is present, the request hash changes by design (new input) and
  the run records the normalized-input/effective/readback object. `requested`
  is intentionally not used as a field name because the normalized project has
  already materialised schema defaults.
- Snapshot JSON and supervisor manifest gain an `oxidation` object only when the
  block is present; deferred materialisation reproduces it.
- No existing oxidation schema field, default or API route changes.

## 7. Tests

`tests/test_oxidation_advanced.py` covers:

- catalog/defaults and the four native setter names;
- omitted-block hash stability against the base commit;
- explicit-default no-op in 2D and 3D;
- Si110 native enum/readback and distinct deterministic 2D/3D surfaces;
- at least two (in fact all four) accepted setters changing a 2D surface;
- 3D advanced acceptance and recorded effective values;
- unknown keys, out-of-range/non-finite values, coupled tolerance ordering,
  unsupported orientation, inert/absent candidates, non-oxidation placement and
  dimension-specific excluded bounds;
- save/reopen, API run, snapshot manifest/snapshot readback and replay.

## 8. Limitations

- The four solver setters have no native getters in 4.6.2. Their `effective`
  values are the validated values passed to the named setters; behavioural
  evidence is a surface-hash probe, not a post-run getter.
- At the workbench's stress-free default (`reactionActivationVolumeM3=0`,
  `diffusionActivationVolumeM3=0`), the 3D coarse-grid numerics can converge
  identically for the solver controls. The 2D probes are the behavioural
  evidence; 3D only proves acceptance/recording and is not claimed to change the
  surface for every setter.
- Mask mechanics and GPU oxidation remain unexposed. There is no silent
  fallback for either.
