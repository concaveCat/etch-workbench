# ViennaPS 4.6.2 → 4.7.0 migration

Status: **ViennaPS 4.7.0 / ViennaLS 5.10.0 is the default runtime (switch
validated on `task/capability-v470-default`, base `ceb18c0`). The 4.6.2
runtime is retained for replay, legacy runs and the two capabilities 4.7.0
does not provide.** Sections 1–9 record the original isolated-evaluation work
on `task/v470-migration` (base `1015266`); section 10 records the default
switch and its evidence.

## 1. Install method and versions

The official PyPI 4.7.0 release is used, not the local `/root/ViennaPS`
development snapshot (`7a8b17f`, 2026-09-18, CMake version already 4.7.0). The
upstream release tag is `v4.7.0` = `f65c1dce102cf4c92769f3bf8a22fb1f6d933abe`.

```bash
cd /root/wb-v470
python3.12 -m venv .venv            # isolated, no system-site-packages
.venv/bin/python -m pip install -r requirements-v470.lock
```

| Component | Version | Source |
|---|---|---|
| Python | 3.12.14 | `/usr/bin/python3.12` |
| ViennaPS | 4.7.0 | PyPI wheel `viennaps-4.7.0-cp312-...whl`, 27,712,192 B |
| ViennaLS | 5.10.0 | PyPI wheel `viennals-5.10.0-cp312-...whl`, 8,295,226 B |
| FastAPI / Pydantic / Uvicorn | 0.162.0 / 2.14.2 / 0.54.0 | `requirements-v470.lock` |
| venv size | 127 MiB | CUDA-enabled (`libViennaLS_GPU.so`, `ptx/`) |

`viennaps 4.7.0` metadata requires `ViennaLS>=5.10.0`; `pip check` is clean.
System packages and the main `.venv` were not modified.

## 2. API differences

Full class/member dumps were compared between 4.6.2 and the PyPI 4.7.0 wheel
(102 vs 125 viennaps classes, 83 vs 80 viennals classes).

Added in 4.7.0: `LevelSet`, `MakeGeometry`, `Box`, `Sphere`, `Cylinder`,
`Plane`, `VTKWriter`, `BooleanOperation*`; `IonImplantation`, `Anneal`,
`AnnealMode`, the `Implant*`/`PearsonIV*` models, `ImplantDoseControl`,
`ScreenEnergyLoss`; `NetDoping`, `SheetResistance`; modeldb roots.

Removed or renamed:

| 4.6.2 | 4.7.0 |
|---|---|
| `viennaps.GpuPreconditioner` | removed |
| `viennals.GpuPreconditioner`, `ILU0`, `Jacobi` | removed |
| `viennals.OxidationConstrainedAmbient`, `OxidationMaskBending` | removed |
| `OxidantType.Dry` / `.Wet` | `OxidantType.DRY` / `.WET` |
| `Oxidation.setGpuPreconditioner`, pressure/stokes iteration+tolerance setters, `setSimplePressureRelaxation`, `setSimpleVelocityRelaxation`, all `setMask*` mechanics setters | removed |
| `OxidationDeformationParameters` pressure/stokes/relaxation fields | removed |
| `OxidationMaskParameters` material/contact/anchor/creep fields | removed; `viscosity`, `shearActivationVolume` added |

Added and used by the migration: `DenseCellSet.setScalarData`,
`Oxidation.setInitialTimeStep/setRegrid/setMaskBarrierOnly/...`,
`RayTracingParameters.minRayDistance`.

Unchanged and re-verified: `GDSReader`, `GDSGeometry`, `MakeStack`,
`Planarize`, `DenseCellSet`, `Process`, and all 91 `Material` names/IDs
(max built-in 176, so custom IDs 177–240 remain valid).

### Binding pitfall

4.7.0 binds the 2D call `ps.Domain(gd, xExtent, boundary)` to the
`(gd, xExtent, yExtent)` overload, silently dropping the boundary; 4.6.2 binds
the boundary overload. The keyword form `ps.Domain(gd, xExtent,
boundary=boundary)` is unambiguous on both and is used in `worker/simulate.py`
and all test fixtures.

Unrelated, pre-existing on both versions: an all-reflective 2D/3D domain makes
`MakePlane.apply()` hang; an `INFINITE_BOUNDARY` is required on the open axis.

## 3. Version policy and runtime gates

`backend/engine.py` is the single source of runtime identity:

- `SUPPORTED_ENGINE_VERSIONS = ("4.6.2", "4.7.0")`
- `engine_version()` / `viennals_version()` read the installed metadata
- `EVIDENCE_ENGINE_VERSION = "4.6.2"` keeps archived matrices/replays pinned to
  the run that produced them
- `ENGINE_REGISTRY_VERSION = "4.6.2"` keeps on-disk material catalogs readable
  by the unchanged 4.6.2 service; readers accept both supported versions
- `PATCHED_RUNTIME_VERSIONS = ("4.6.2",)` makes the ABI-bound patched GPU
  runtime explicit

Current-runtime metadata (`/api/materials` catalog, specialized model catalog)
reports `engine_version()`. `backend/materials.py`, `backend/native_build.py`,
`backend/validation.py` and the stage 6/8 assertions no longer hardcode the
installed version. Comments and historical evidence strings that intentionally
describe the 4.6.2 measurement were left intact.

### ALD `surfaceDiffusion` gate (4.7.0)

An **enabled** `steps[].surfaceDiffusion` on `specialized:ald` is rejected
during `validate_project()` on 4.7.0, before a worker or run is created:

```
422 steps[0].surfaceDiffusion: ViennaPS 4.7.0 has a confirmed native hang in
the ALD SurfaceDiffusionParameters stencil; the worker process would never
return. Use the ViennaPS 4.6.2 runtime, or remove or disable this block.
```

- 4.6.2: unchanged. The ALD schema + native classes run 14/14 green on the
  4.6.2 interpreter, including the two golden/knob native cases that are
  excluded on 4.7.0. (The two API persistence cases need the 4.6.2 worker venv
  and were not rerun from this 4.7.0 worktree.)
- 4.7.0: fail fast for enabled blocks; an explicit `{"enabled": false}` token
  stays accepted because it is inert and never reaches the native binding.
- The field is not silently disabled: the request is rejected, and the error
  names the runtime and the native class.
- The **worker builder** calls the same `ald_surface_diffusion_error()` helper
  before constructing `SurfaceDiffusionParameters`. A hand-edited `input.json`
  that bypasses API validation therefore fails fast with the same message
  instead of reaching the hanging native path. The direct-worker regression
  covers 4.7.0 rejection and 4.6.2 acceptance.
- Request hashes for projects without `surfaceDiffusion` are unchanged
  (`test_omitting_the_object_keeps_the_base_request_hashes`).
- Worker-side defense: the worker still refuses an inert ALD configuration and
  a radius above 8 grid intervals if a hand-edited project bypasses validation.

### `advancedOxidation` gate (4.7.0)

`probe_oxidation_setters.py` re-ran the Workbench block on both runtimes
(2D Si/SiO₂, dry, 1000 °C, 0.166 h, 0.03 h steps):

| Control | 4.6.2 | 4.7.0 |
|---|---|---|
| `cflFactor=0.05` | changes surface | **changes surface** |
| `mechanicsTolerance=0.05` | changes surface | **inert** (identical hash) |
| `couplingTolerance=0.2` (δ=0.02) | changes surface | **inert** (identical hash) |
| `simplePressureRelaxation=0.1` | changes surface | **setter removed** |

Consequently, on 4.7.0 a request that **changes** any of those three controls is
rejected with a 422/validation error naming the field, the runtime and the
reason. Materialised defaults remain accepted so normalized projects and
replays stay valid; the worker never sends the removed setter and records
`pressureRelaxationSetter: "removed by the installed viennaps (4.7.0)"`.
On 4.6.2 all four controls keep their previous behaviour.

### Replay / old-run policy

Old 4.6.2 runs are **never reinterpreted** under 4.7.0:

- `/api/runs/replay` returns 422 when `executionRequirements` versions differ,
  with `requiredVersions`, `installedVersions` and the message
  “…Old runs are never reinterpreted under a different runtime: start the
  matching ViennaPS 4.6.2 runtime to replay this run.”
- replay package import returns 409 with the same policy text.
- native Domain state continuation and the supervisor raise
  `ReplayMismatch` naming both version pairs.
- To replay an old run, use the retained 4.6.2 environment. Full dual-runtime
  execution is intentionally not implemented; the rejection path is explicit
  and tested.

Deferred snapshots follow the same rule. `worker/materialize_snapshot.py`
regenerates `<step>.json` from the saved native `.vpsd` on first read; both
`backend/snapshots.ensure_materialized` and the worker compare
`input.json.expectedVersions` against the installed runtime before staging or
reading native state. A mismatch returns **409** on snapshot GET and run
download with `message`, `requiredVersions` and `installedVersions`; no
snapshot is cached. Runs produced by the matching runtime still materialize
normally, and an already-cached JSON is served as stored data without a native
read.

## 4. Bounded probes

All probes run through `scripts/bounded_child.py` (60 s timeout, 4 GiB RSS
polling limit, 1 MiB log) or a direct `timeout` for the deliberate hang repro.
Scripts are in `scripts/migration-probes/`; outputs in `var/probes/`.

| Probe | 4.7.0 | 4.6.2 |
|---|---|---|
| masked Box/Sphere (`probe_masked_distributions.py`) | pass | official pattern fails (`ps.LevelSet` absent); 4.6.2-compatible variant passes |
| manual implant + anneal (`probe_implant_anneal.py`) | pass: dose conserved 78.313341→78.313352, 89.2 % active | n/a (`IonImplantation`/`Anneal` absent) |
| GDS + `MakeStack` + cell set + oxidation (`probe_geometry_oxidation.py`) | pass: 6 structures/7 layers, 500-cell scalar round-trip, 0.01337 µm oxide | pass for the same calls without the 4.7.0-only classes |
| GPU `GPU_DISK` (`probe_gpu.py`) | pass (execution): 64 rays, 265 MiB; CPU/GPU not bit-identical | pass |
| oxidation setters (`probe_oxidation_setters.py`) | cfl works; mechanics/coupling inert; pressure unbound | all four change the surface |
| ALD hang (`probe_ald_hang.py`) | **hangs reliably** (3/3, 10 s timeout) | returns in 0.01 s |

The ALD repro needs both the non-default `SurfaceDiffusionParameters`
(`radius=0.1`, `sigmaNormal=0.2`, `kNeighbors=8`, `stabilityFactor=0.5`) and
the pinned `RayTracingParameters`; without the ray block the same case returns.
It reproduces on 4.7.0/5.10.0 only.

## 5. Test results

`330` tests are collected in this worktree (5 skipped). With the 4.7.0 venv,
`VIENNAPS_WORKBENCH_DATA_DIR=$PWD/var/test-v470`, `WORKBENCH_PORT=8767`:

```
Ran 328 tests in 163.1s
FAILED (failures=3, errors=58, skipped=5)
```

That is 262 passed, 3 failed, 58 errored, 5 skipped, plus the 2 excluded native
ALD cases.

Two native ALD tests (`test_golden_2d_and_3d_...` and
`test_the_knob_changes_the_surface_...`) are excluded by the runner on 4.7.0
because they intentionally exercise the hanging native path; the request is
now rejected before the worker, and the native hang is covered by
`probe_ald_hang.py`. Excluding them is not a green suite: the reported numbers
above are the actual 4.7.0 result for the remaining tests.

Breakdown of the 61 failing/erroring ids:

| Class | Count | Detail |
|---|---|---|
| Missing untracked evidence in this worktree | **61** | `docs/*-evidence/...`; every path exists in the main worktree. Not regressions. |
| Real code/API regressions | **0** | 3 failures + 58 errors are all missing evidence. |

Focused results after the hardening changes:

- `tests.test_oxidation_advanced`: 25/25 pass (both runtime branches tested via
  version patching).
- `tests.test_ald_surface_diffusion` schema class: 8/8 pass, including the
  explicit 4.6.2-acceptance and 4.7.0-rejection cases.
- `tests.test_api_integration`: 16/16 pass (4 skipped).
- `tests.test_domain_continuation`: 2/2 pass; state metadata records the
  installed version instead of a literal.
- `tests.test_stage6`: pass; catalog `engineVersion` follows the runtime.
- `tests.test_engine_cellset`, `test_plasma_advanced`,
  `test_engine_batch1_specialized`, `test_cmp_measured_thickness`: pass.
- `tests.test_snapshot_runtime_policy`: 5/5 pass (foreign-runtime GET/download,
  typed direct error, worker pre-read rejection, matching-runtime success).
- Direct ALD worker gate: 4.7.0 rejection and 4.6.2 acceptance pass.

## 6. Fixes and files

- `backend/engine.py` — runtime identity, supported-version sets, version
  predicates, restricted oxidation controls.
- `backend/validation.py` — ALD/oxidation gates, version-qualified warnings,
  historical evidence constant.
- `backend/materials.py`, `backend/specialized_models.py` — current-runtime
  metadata, registry compatibility, `runtimeGate` catalog metadata.
- `backend/native_build.py` — explicit patched-runtime version set.
- `backend/main.py`, `backend/supervisor.py` — explicit replay policy/errors.
- `backend/snapshots.py`, `worker/materialize_snapshot.py` — deferred-snapshot
  runtime gate before native read; 409 policy detail; matching-runtime read
  unchanged.
- `worker/simulate.py` — keyword 2D boundary.
- `worker/specialized.py` — oxidant enum fallback, guarded/version-checked
  oxidation setters, removed-setter record.
- Tests and fixtures — version-aware assertions, gated cases, 2D boundary
  keyword, no-op/disabled acceptance, direct-worker ALD gate, snapshot policy.
- `scripts/migration-probes/probe_ald_hang.py`,
  `probe_oxidation_setters.py`, plus the earlier capability probes.
- `requirements-v470.lock`.

## 7. Remaining blockers to a 4.7.0 default

1. **Native ALD hang** (upstream bug). Until fixed, `surfaceDiffusion` is
   unusable on 4.7.0; the gate prevents a hung worker but the feature is
   unavailable. Issue draft: `docs/upstream-ald-surface-diffusion-hang.md`.
2. **Oxidation control parity.** Only `cflFactor` remains effective. The
   catalog still advertises mechanics/coupling/pressure fields; the UI should
   hide or mark them version-qualified before a default switch.
3. **Evidence coverage.** All capability/evidence matrices are 4.6.2
   measurements. A default switch needs a fresh bounded 4.7.0 evidence pass or
   an explicit “verified on 4.6.2 only” policy in the UI.
4. **Replay/continuation.** Old runs require the 4.6.2 runtime; a default
   switch without a retained 4.6.2 deployment would make history read-only
   (rejected, not broken).
5. **GPU multi-ion.** The patched 4.6.2 runtime is ABI-bound to ViennaLS 5.8.5
   and is rejected on 4.7.0; stock GPU single-particle works, multi-ion is
   unverified.

## 8. Recommendation

Keep 4.6.2 as the production default. 4.7.0 is now safe to run as an isolated
evaluation runtime: the known hang fails fast with a clear 422, the removed
oxidation setters are gated, and old runs are explicitly refused rather than
reinterpreted. Revisit the default after the upstream ALD fix (or an accepted
“no surfaceDiffusion on 4.7.0” product decision), a re-scoped oxidation UI, and
a 4.7.0 evidence pass.

## 9. Risks

- **GPU:** stock 4.7.0 runs `GPU_DISK`, but the multi-ion source patch does not
  carry over; GPU multi-ion must stay on 4.6.2 or be re-validated.
- **Snapshots/replay:** 4.6.2 runs become read-only once 4.6.2 is retired.
- **Request hashes:** unchanged for projects without the gated fields; adding
  or removing `advancedOxidation` fields changes hashes for projects that use
  the block. No schema change was made in this pass.
- **Artifacts:** isolated venv 127 MiB, wheels 36 MiB; GPU PTX payload inflates
  container images.
- **Evidence:** 4.6.2 evidence remains the only accepted proof set.

## 10. Default switch (2026-10-31)

Branch `task/capability-v470-default` (base `ceb18c0`), `.venv` → the isolated
4.7.0 venv (`viennaps 4.7.0` / `viennals 5.10.0`).

### 10.1 Decision

- **Default runtime: ViennaPS 4.7.0 / ViennaLS 5.10.0** (`requirements-v470.lock`).
- **Retained runtime: ViennaPS 4.6.2 / ViennaLS 5.8.5** (`requirements.lock`).
  It stays mandatory for three things and is never deleted:
  1. replay and snapshot/continuation of runs produced before the switch,
  2. the patched multi-ion GPU build (`PATCHED_RUNTIME_VERSIONS`), and
  3. the 2D `FaradayCageEtching` kernel.
- Old runs are **not reinterpreted**: replay, deferred snapshot materialisation
  and native Domain continuation keep returning the explicit
  required/installed version pair.
- Three capabilities are **version-gated instead of silently degraded** on
  4.7.0: ALD `surfaceDiffusion` (hang), the advanced-oxidation controls
  (inert setters), the 2D Faraday cage etch (zero velocity), and multi-ion GPU
  (dropped ions). See 10.4.

### 10.2 Test results

`VIENNAPS_WORKBENCH_DATA_DIR=$PWD/var/test-v470 WORKBENCH_PORT=8767
.venv/bin/python -m unittest discover -s tests -t . -v`:

```
Ran 447 tests in 328.8s
FAILED (failures=2, skipped=13)      # 432 passed
```

Evidence is not in git, so the run needs the main worktree's
`docs/*-evidence/` and `var/stage9-*` run artifacts referenced read-only
(copied into this worktree for the run and deleted afterwards).

- **2 failures, both pre-existing, neither version-specific**:
  `tests.test_stage5.Stage5Acceptance.test_gds_upload_selection_native_geometry_portable_package_and_replay`
  fails with `GDS 版图边界 [0.2505, 7.5] 超出当前计算域（xExtent=8）` on **both**
  runtimes (reproduced with the 4.6.2 interpreter against the same tree) and
  then cannot import the package it never produced. Open item, not a 4.7.0
  regression.
- **13 skips**: 2 gated ALD native cases, 4 `WORKBENCH_GPU_TEST` host-probe
  gated API cases, 5 2D Faraday cases gated on 4.7.0 (10.4), 1 archived
  `cpu-nan-debug` guard, 1 GPU matrix run-artifact audit whose `var/` inputs
  are not archived in a worktree.
- A 4.6.2 control run of the same tree cannot be used for the API tests: the
  supervisor starts the worktree `.venv` (4.7.0) and every worker correctly
  refuses the version mismatch (110 such failures in the control). Only
  in-process/native tests are comparable, and they were compared individually
  (10.4, 10.5).

### 10.3 Capabilities verified on 4.7.0

Every row below is a real native run inside the suite; the named test is the
one that proves it.

| Capability | Status on 4.7.0 | Evidence |
|---|---|---|
| A4 masked etch (positive displacement still gated) | pass | `test_particle_shape`: `test_masked_etch_moves_the_substrate_but_not_the_mask`, `test_mask_changes_the_geometry_versus_an_unmasked_native_run`, `test_masked_deposition_segfaults_with_any_non_empty_mask` |
| GDS native layer bodies + padding | pass | `test_gds_native_bridge.NativeBodyComparison.test_native_2d_and_3d_bodies_match_direct_reference`, `test_gds_boundary` (layer-1 body is now native; see 10.5) |
| GDS proximity blur + budget | pass | `test_gds_native_bridge.BlurContract`, `GdsNativeBridgeRuns.test_2d_blur_threshold_effect_and_determinism`, `test_3d_blur_uses_the_native_body_and_reports_values`, `test_blur_overflow_and_underflow_are_422_not_500` |
| `nvFactors` (selective epitaxy) | pass | `NativeSelectiveEpitaxyNvFactors` (3 native/reference cases + validation) |
| GTD 3D geometric trench | pass | `NativeGeometricTrenchDeposition.test_3d_trench_matches_a_direct_native_reference` |
| `coverageTolerance` | pass | `FaradayCpuReplay.test_explicit_coverage_tolerance_cpu_run_replays`, `test_coverage_tolerance_rejects_invalid_range_and_wrong_models` |
| `GPU_TRIANGLE` | pass | `test_gpu_triangle_real_2d_and_3d_product_direct_and_replay` (2D/3D, product vs archived direct reference within 1e-6 µm; exact on the 4.6.2 run that produced it) |
| any-axis `Slice` | pass | `test_slice_x_y_z_axis_native_coordinates_and_replay`, `test_slice_position_uses_normal_axis_extent`, `test_slice_extruded_fin_uses_actual_y_range`, `test_slice_half_domain_positions_are_bounded` |
| half/quarter shapes | pass | `test_native_half_shapes_product_direct_symmetry_and_replay` (2D trench/fin, 3D trench/box/hole), `test_boolean_fin_route_keeps_half_fin`, `test_invalid_half_shape_combinations_are_rejected` |
| Faraday cage 3D (CPU-only) | pass | `NativeFaradayCage.test_real_3d_cpu_run_etches_the_exposed_film`, `FaradayCpuReplay.test_faraday_cage_cpu_run_replays` |
| Faraday cage 2D (CPU-only) | **gated on 4.7.0** | see 10.4 |
| per-layer trench | pass | `test_capability_layers.PerLayerTrenchNativeReference.test_per_layer_width_taper_and_mask_are_applied` |
| 4.7.0-only implant / anneal / doping / sheet resistance | **not integrated** | no Workbench schema, worker builder or test uses `IonImplantation`, `Anneal`, `NetDoping` or `SheetResistance`. Only the bounded migration probe `probe_implant_anneal.py` exercised them (dose conserved 78.313341→78.313352, 89.2 % active). They are not advertised, so the default switch neither adds nor removes them. |

### 10.4 Two new 4.7.0 gates (measured, not assumed)

**2D `FaradayCageEtching` returns no etch on 4.7.0.** Same tree, same model
builder, same inputs (`worker/probe_faraday_2d.py --case`), only the wheel
differs:

| Runtime | cageAngle / tiltAngle | beforeTop | afterTop | etchedDepth |
|---|---|---|---|---|
| 4.6.2 | 0.2 / 0.0 | 0.8 | 0.7993287183 | **0.000671 µm** |
| 4.7.0 | 0.2 / 0.0 | 0.8 | 0.8 | **0.0** |

The 4.7.0 call completes in 1.6 ms with every parameter read back correctly and
all three level sets intact, so the run reports success while nothing moves.
The suite caught it in five independent direct-native assertions (both 2D
anchors and the `-0.2°`/threshold cases). `backend/engine.faraday_2d_error()`
now rejects 2D `faradayCage` on ≥ 4.7.0 with the measured depth, and
`faraday_cage_2d_engine_error()` (API validation + `build_specialized_model`)
returns it before a worker exists. 3D is unaffected and still runs.

**Multi-ion GPU drops every ion after the first.** Stock wheel probe
`scripts/migration-probes/probe_stock_multiion_gpu.py`, rate callback weighted
on the *second* ion only, 96 rays, fixed seed, RTX 3060 UUID
`GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d` (bounded child, 60 s / 4 GiB / 1 MiB):

| Runtime | Engine | Dim | Ions | Flux callback vector | Native flux fields | Etch depth |
|---|---|---|---|---|---|---|
| 4.7.0 | CPU_DISK | 2 | 2 | `[2]` | `ion00`, `ion11` | 0.4963 |
| 4.7.0 | CPU_DISK | 3 | 2 | `[2]` | `ion00`, `ion11` | 0.4964 |
| 4.7.0 | GPU_DISK | 2 | 2 | `[1]` | `ion000` | **0.0** |
| 4.7.0 | GPU_DISK | 3 | 2 | `[1]` | `ion000` | **0.0** |
| 4.7.0 | GPU_TRIANGLE | 2 | 2 | `[1]` | `ion000` | **0.0** |
| 4.7.0 | GPU_TRIANGLE | 3 | 2 | `[1]` | `ion000` | **0.0** |
| 4.7.0 | GPU_DISK | 2 | 4 | `[1]` | `ion000` | **0.0** |
| 4.7.0 | GPU_DISK | 3 | 4 | `[1]` | `ion000` | **0.0** |
| 4.7.0 | GPU_DISK | 2 | 2, weight on ion 0 | `[1]` | `ion000` | 0.3580 |

Control: the same probe with `--active-index 0` etches, which proves the run
itself is fine and only the extra species are lost. Replay is deterministic
(two separate processes, identical final surface hash); the CUDA UUID readback
equals the requested `GPU-5532bb0f-…` for every GPU case. A second
`gpu.Context.createContext()` inside one process aborts OptiX with code 7001,
so replay checks use two processes.

This is the same defect the 4.6.2 patch fixed, not a new 4.7.0 regression: the
4.6.2 wheel behaves identically and the Workbench only got multi-ion GPU from
the patched build. The patch is ABI-bound to ViennaLS 5.8.5.

### 10.5 Multi-ion GPU policy

- **No silent loss.** On ≥ 4.7.0 a schema-7 `multiParticle` step with more than
  one ion that resolves to a GPU engine is rejected at validation
  (`steps[i].species`) by `backend/engine.multi_ion_gpu_error()`, naming the
  measured behaviour and both escapes: the 4.6.2 patched runtime, or
  `CPU_DISK` on 4.7.0 (which is verified above).
- `worker/simulate.py` calls the same helper from the process path
  (`execute`), so a hand-edited `input.json` cannot reach the lossy binding.
- **Single-pass flux path (fixed after the default switch).** The first cut of
  this gate only covered `execute`, which left three ways to reach a GPU flux
  call with two or more ions:
  1. `worker/simulate.py` `calculate_single_flux(..., engine="GPU_DISK",
     flux_strategy=None)` (and `GPU_TRIANGLE`) went straight to
     `Process.calculateFlux()`;
  2. the species-completeness check only ran for
     `native-multi-ion-flux-v1`, so a `None` strategy skipped it and accepted
     the truncated native field set;
  3. `backend/supervisor.py` keyed the gate off `needs_native_patch`, so a
     flux replay carrying `per-species-native-mesh-merge-v1` and **no**
     fingerprint (that route needs no patched build) was accepted as pending.
  All three now call the shared policy: `worker.simulate.multi_ion_gpu_step()`
  / `require_multi_ion_gpu_policy()` run at the top of `calculate_single_flux`
  before any native model, `deposit` duplication or GPU context, for
  `GPU_DISK`/`GPU_TRIANGLE`, 2D/3D and every strategy including `None`;
  `require_multi_ion_flux_fields()` checks the species fields of any
  non-merge multi-ion GPU flux (`flux_strategy=None` included) and rejects the
  known truncated field set; `Supervisor.start()` rejects every multi-ion GPU
  step in `actual_steps` before `needs_native_patch` is consulted, new run or
  replay, process or flux; `POST /api/runs/replay` applies the same check for
  API parity.
- The per-species merge route (`per-species-native-mesh-merge-v1`) has 4.6.2
  evidence only, so it is gated on ≥ 4.7.0 as well; the policy text says so.
- Single-ion and multi-ion **CPU_DISK** stay available on 4.7.0. On 4.6.2
  nothing changes: the patched runtime path, the operator-split strategy and
  the native strategy are untouched.
- `PATCHED_RUNTIME_VERSIONS` stays `("4.6.2",)`; `native_build.py` still fails
  closed if a 4.7.0 package is put in `VIENNAPS_PATCHED_RUNTIME_DIR`.

### 10.6 Version-qualified tests and evidence

- `tests/test_native_multiion_policy.py` — new policy class: 4.7.0 rejects,
  4.6.2 accepts (both branches exercised through `backend.engine.engine_version`).
  The historical split-route audit pins its recorded 4.6.2 runtime.
  `MultiIonGpuFluxPolicyTests` covers the single-pass path: direct
  `calculate_single_flux` rejection for `GPU_DISK`/`GPU_TRIANGLE` × 2D/3D ×
  `{None, per-species, native}` (called with `domain=None`, which proves the
  gate runs before any native work), the non-gated matrix
  (`CPU_DISK`/`CPU_TRIANGLE`/single-ion/non-multiParticle), truncated native
  flux rejection for `None` and native strategies, and a real 2D `CPU_DISK`
  two-ion flux that still emits `ion00`/`ion11`.
- `tests/test_api_integration.py` —
  `test_multi_ion_gpu_flux_replay_is_rejected_before_a_worker_starts` drives
  `Supervisor.start("flux", ...)` directly with a `per-species-native-mesh-merge-v1`
  replay and no fingerprint, asserts the policy text, that validation refuses
  the same project with the same text, and that no run directory is created.
- `tests/test_engine_batch1_specialized.py` — 2D Faraday native/replay cases
  are `skipUnless(FARADAY_2D_ETCHES)`; a new
  `test_2d_policy_matches_the_installed_runtime` asserts the rejection and keeps
  3D accepted; contracts measured on 4.6.2 pin it through `measured_engine()`.
- `tests/test_gds_boundary.py` — layer 1 now asserts the native body on ≥ 4.7.0
  (4.6.2 returns an empty native body and needs the fallback) after the
  unchanged geometry assertions.
- `tests/test_native_capability_release.py` — `GPU_TRIANGLE` product/direct
  comparison is exact only against a reference produced by the same runtime;
  otherwise it is bounded to 1e-6 µm and the deviation is recorded in the
  evidence JSON.
- `tests/test_stage9.py` — matrix audits pin the 4.6.2 runtime their rows were
  measured on; the run-artifact audit skips (with the count) when `var/`
  artifacts are absent instead of claiming a code regression.

### 10.7 Files changed for the switch

- `backend/engine.py` — `DEFAULT_ENGINE_VERSION`/`LEGACY_ENGINE_VERSION`,
  `MULTI_ION_GPU_STOCK_VERSIONS`, `multi_ion_gpu_error()`, `faraday_2d_error()`.
- `backend/specialized_models.py`, `backend/validation.py`,
  `backend/supervisor.py`, `worker/simulate.py` — shared gates and fail-fast.
- `requirements-v470.lock` (default lock), `requirements.lock` (retained
  legacy), `requirements.in`, `README.md`, `README.zh-CN.md`,
  `docs/compatibility.md`, this section.
- `scripts/migration-probes/probe_stock_multiion_gpu.py` and the tests above.

Regenerate the multi-ion table with
`scripts/migration-probes/probe_stock_multiion_gpu.py` under
`scripts/bounded_child.py` (`--dimension`, `--engine`, `--ions`,
`--active-index`); the 2D Faraday pair with
`python -m worker.probe_faraday_2d --case '{"dimension":2,"cageAngle":0.2,"tiltAngle":0.0,"engine":"CPU_DISK"}'`.

## 11. Implant / anneal / doping capability (schema 7)

The 4.7.0-only `IonImplantation`, `Anneal`, `NetDoping` and `SheetResistance`
classes are exposed as three additive schema-7 step models (`implant`,
`anneal`, `analysis`). They operate on the Domain cell set, never on the
surface, and are rejected on 4.6.2 before a worker starts. The contract, the
bounded native evidence (`worker/probe_implant_native.py`) and the list of
intentionally unexposed setters are in
[`docs/capability-implant.md`](capability-implant.md).
