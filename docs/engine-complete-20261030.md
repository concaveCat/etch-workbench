# ViennaPS Workbench — engine completion archive (2026-10-30)

This archive closes the backend/API-only engine exposure work on top of the installed
ViennaPS 4.6.2 / ViennaLS 5.8.5 runtime. The B3–B5 engine phase and this archive commit
touch no `frontend/` files; the user's new frontend is expected to consume the JSON
schema, validation, worker metadata and API. Earlier A1/A3 merges in this history
contain frontend integration (CSV upload and dimension-conversion notices); those
changes are preserved as-is, are outside the B3–B5 backend/API work, and should be
reviewed or replaced with the new frontend as needed.

## Final state

| Item | Value |
| --- | --- |
| Engine code freeze | `e0440cb` (`merge: close B5 oxidation checkpoint findings`) |
| Archive tag | `archive/engine-complete-20261030` |
| Tarball | `/root/archives/viennaps-workbench-engine-complete-20261030.tar.gz` |
| Full suite | **318 tests OK, 5 skipped** |
| Capability matrix | **62 exposed / 9 advanced-not-exposed / 7 engine-model-not-exposed / 18 internal**, 96 class rows |
| Review log | [`engine-checkpoints-20261030.md`](engine-checkpoints-20261030.md) |
| Machine-readable matrix | [`engine-capability-matrix.json`](engine-capability-matrix.json) |

Every engine batch below has an independent review checkpoint. B1 was reviewed by the
DeepSeek V4.1 Flash fallback because codex returned a usage-limit error; B2–B5 used
codex. The final B5 checkpoint found no remaining Blocker, Major or Minor. The full
suite was run on `main` before the archive documentation commit.

## Delivered engine batches

| Batch | Capability | Final merge | Contract |
| --- | --- | --- | --- |
| A1 | `FaradayCageEtching` as a 3D specialized model | `0fc3ae3` | specialized catalog / capability matrix |
| A2 | `GeometricTrenchDeposition` as a 2D specialized model | `0fc3ae3` | specialized catalog / capability matrix |
| A3 | `CSVFileProcess` + `RateGrid` with uploaded, hashed CSV assets | `d2d81a0` | [`csv-process-contract.md`](csv-process-contract.md) |
| B1 | Plasma `nativeParameters.advanced` allowlists and parameter-object overloads | `cf46877` | [`plasma-advanced-parameters-contract.md`](plasma-advanced-parameters-contract.md) |
| B2 | Multi-`RateSet` directional model (`steps[].rateSets[]`) | `eb5a40c` | [`directional-contract.md`](directional-contract.md) |
| B3 | `SurfaceDiffusionParameters` on ALD coverage | `69ed1b0` | [`ald-surface-diffusion-contract.md`](ald-surface-diffusion-contract.md) |
| B4 | Generic per-step `DenseCellSet` output (`outputSettings.cellSet`) | `b9ed53e` | [`engine-capability-matrix.md`](engine-capability-matrix.md) |
| B5 | `SiliconOrientation.Si110` and `steps[].advancedOxidation` | `e0440cb` | [`oxidation-advanced-contract.md`](oxidation-advanced-contract.md) |

All optional blocks follow the same rules: omitted means the historical request hash
and artifacts are unchanged; explicit native defaults are a behaviour no-op; unknown
keys, non-finite values and out-of-range values are rejected; worker metadata records
the requested/normalized values, effective values and native insertion point.

## GPU status

GPU support was not removed or narrowed by this archive. The GPU capability matrix
still contains the verified `GPU_DISK` signatures for the models and dimensions that
passed host probes; GPU selection remains capability-gated and never silently falls
back to CPU.

The new B5 oxidation controls are **CPU-only**. `GpuMode` and `GpuPreconditioner` for
oxidation remain unexposed because no verified host run exists. The geometry-only
FinFET flow that follows this archive is also CPU-only: it contains no flux process
that could use `GPU_DISK`.

## Explicitly not exposed

### Engine/model limits

- GPU oxidation (`GpuMode`, `GpuPreconditioner`) — no verified host evidence.
- Oxidation mask mechanics (`setMaskParameters` and `setMask*`) — the controlled 2D
  probe collapsed the oxide level set; no verified LOCOS output.
- Legacy `OxidationParameters` / `psOxidation` / `psProcessContext` — not bound by the
  installed 4.6.2 Python module.
- Oxidation iteration caps, pressure/stokes tolerances, `setSimpleVelocityRelaxation`,
  `setMaxGridPoints`, `setSolveBounds` and the model-level save helpers — inert,
  unsafe, non-independent or output-only; recorded with probe evidence in the B5
  contract.
- Generic `DenseCellSet` continuation (`readCellSetData`), below-surface cell sets and
  arbitrary user scalar labels — only `Material` / `FillingFraction` and a cell set
  above the surface are exposed.
- Plasma `Substrate.B_ie`, CF4O2 SiGe inert yield fields, and the SF6C4F8 passivation
  family — inert or native-crash paths.
- `RateGrid.setCustomInterpolator`, `MultiParticleProcess.setRateFunction` callbacks,
  `ProcessModel`/`ProcessParams`/`AdvectionCallback` extension hooks — deliberately
  never exposed because they would execute arbitrary Python in the worker.
- `GPU_LINE` — not probed and not exposed. `GPU_TRIANGLE` is exposed only for the archived 2D/3D specialized sf6o2 etch/reflective stack pair via schema-7 `solver.gpuEngine`; unproven combinations are rejected.

### Batch A4 (particle shapes)

The `task/engine-particles` branch (`afe9f46`) is intentionally not merged. It exposes
only the maskless Box/Sphere distribution; masked Box/Sphere and
`CustomSphereDistribution` remain native-crash or inert blockers. This archive does
not change, merge or instruct that branch.

### Physics outside this engine

The 4.6.2 engine does not provide lithography, implantation, anneal, silicide, stress,
doping or electrical simulation, and `Planarize` is a geometric cutoff rather than a
true CMP model. The geometry-only FinFET flow therefore stops at M0 geometry and makes
no device-physics claim.

## Verification

```bash
cd /root/viennaps-workbench-cpu-gpu
VIENNAPS_WORKBENCH_DATA_DIR=$PWD/var/test-engine-final \
  WORKBENCH_PORT=8767 \
  .venv/bin/python -m unittest discover -s tests -t . -v
```

Expected: `Ran 318 tests ... OK (skipped=5)`.

The archive tarball is built from a `git archive` of the tag, then filtered to remove
`var/`, `frontend/dist/`, `.venv/`, `runtime-patched/`, `docs/*-evidence/` and untracked
acceptance directories even where a placeholder or evidence matrix is tracked. The tag
and `main` are pushed to `origin` without force.
