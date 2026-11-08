# Codex checkpoint log

## 2026-10-30 — Engine Batch 1 (FaradayCage + GeometricTrenchDeposition)

- Review command: `codex exec -s read-only ...`
- Session: `01a18a61-da98-7052-bcd9-4c159b309038`
- Reviewed merge: `276101a` (commit `e1e83b4`)
- Initial findings: 2 Major + 3 Minor
  - FaradayCage CPU replay rejected by capability gate
  - GeometricTrenchDeposition fractional `n` could produce NaN
  - Stage 9 expected-model set derived from evidence
  - UI dimension conversion retained incompatible specialized steps
  - Audit claimed `cageAngle=0` equals normal-incidence IBE
- Fixes:
  - `9920efe` / merge `6d7a2fa`: experimental CPU replay helper; integer `n`/positive width+rate; independent Stage 9 expected set; conversion drops unsupported models; audit reference corrected.
  - `9f0e77a` / merge `c7b1850`: depth-span guard; combined removal notices.
  - `27b8036` / merge `c4d240c`: first-enabled-step rule.
  - `e4de2d8` / merge `0fc3ae3`: reject GDS/continuation inputs whose vertical bounds cannot be validated.
- Final codex result: **No remaining Blocker, Major, or Minor within scope**.
- Final full suite on main: **211 tests OK, 5 skipped**.
- Codex session resumed four times (`6d7a2fa`, `c7b1850`, `c4d240c`, `0fc3ae3`); all findings closed.

## 2026-10-30 — Batch A3 (CSVFileProcess + RateGrid)

- Review command: `codex exec -s read-only ...`
- Session: `01a18aa6-8990-76a1-ae1e-e0a4a350ec74`
- Reviewed: `a6060f4` / `8709f36`, fixes `bebb060`, `bbac574`, `cafbfd1`
- Initial findings: 4 Major + 3 Minor
  - BOM/parser-native disagreement (silent zero velocity / LINEAR SIGSEGV)
  - recipe upload input unmounted
  - dimension switching kept incompatible CSV steps
  - legacy GDS overlay mask protection missing
  - offset-blind coverage warning, control chars in fileName, test gaps
- Fixes: canonical ASCII/LF CSV assets with native-load probes; root-mounted file input + Chromium mock test; CSV steps dropped on dimension conversion; shared `worker/mask_materials.py`; offset-aware warnings; filename controls; subnormal/`fin`/legacy-overlay regressions.
- Final codex result: **no remaining Blocker, Major, or Minor in scope**.
- Final full suite: **236 tests OK, 5 skipped**.

## 2026-10-30 — Batch B1 (plasma advanced parameters)

- Reviewer: **DeepSeek V4.1 Flash fallback** (`ses_e74db96c7ffePeOu2BUBcU3zV5`), because codex returned a usage-limit error (`01a18b20-...`).
- Reviewed: `fa6cfd0` / `769f90b`; fix `8ab83b6`; final `a5c1117`, `b93b07e`.
- Initial fallback findings: 1 Blocker + 2 Major + 6 Minor
  - sf6c4f8 positive passivationFlux caused native SIGSEGV
  - cf4o2 SiGe inert fields and sf6c4f8 passivation/beta_P exposed as false knobs
  - advanced defaults differed from flat catalog defaults
  - beta ranges, test gaps, stale matrix, boolean metadata, huge-int overflow, unused import
- Fixer: DeepSeek (same fallback track); reviewer allowed to fix per user instruction.
- Final fallback review: **no Blocker/Major**; two Minor doc/test issues fixed in `b93b07e`.
- Final full suite: **262 tests OK, 5 skipped**.

## 2026-10-30 — Batch B2 (multi-RateSet directional)

- Reviewer: codex (`01a18b90-07db-7cc1-beda-542ffec98e2e`)
- Reviewed: `30c0443` / `e5b4f77`; fix `d5a9332`; final `eb5a40c`
- Initial findings: 1 Major + 2 Minor
  - old frontend round-trip writes scalar placeholders in rateSets mode (out of scope; documented for the new frontend)
  - 2D third-component check pre-normalization
  - per-set visibility test toggled both sets together
- Fixes: contract/API error guidance; normalized |z| check; 2D/3D mixed visibility and mask native references.
- Final codex result: **all three resolved; no remaining Blocker/Major/Minor**.
- Final full suite: **269 tests OK, 5 skipped**.

## 2026-10-30 — Batch B3 (SurfaceDiffusionParameters on ALD)

- Reviewer: codex (`01a18bd9-d364-7ea2-ba32-3163f00d346d`)
- Reviewed: `5d69840` / `3359b9b`; fix `7d02925`; docs `170ef85`
- Findings: no Blocker/Major; 3 Minor (disabled scope bypass, matrix classification/counts, ALD contract wording)
- Fixes: ALD-only placement whenever the object exists; matrix counts reconciled to 61/10/7/18; Coverage-only/purge wording corrected.
- Final: **no Blocker/Major; doc minors closed**.
- Full suite before docs-only merge: **282 tests OK, 5 skipped**.

## 2026-10-30 — Batch B4 (generic DenseCellSet output)

- Reviewer: codex (`01a18c25-4de0-71e0-b568-e94c5c2771b7`)
- Reviewed: `218e824` / `401c12c`; fixes `be847a2`, `b5c38c4`
- Findings: 3 Major (expanded band bound, no-op cell-set outputs, plane elevation in preflight) + 1 Minor (sibling outputs)
- Fixes: level-set width band before allocation; per-step no-op artifacts for eager/deferred; span-based preflight; preserve nativeMetadata rows.
- Final: **no remaining Blocker/Major/Minor**.
- Final full suite: **294 tests OK, 5 skipped**.
- Archive note: the matrix class row for `DenseCellSet` was still marked unexposed after B4; corrected during archive to `exposed`, with counts **62/9/7/18** (96 rows).

## 2026-10-30 — Batch B5 (Oxidation advanced setters + Si110)

- Reviewer: codex (`01a18c99-968f-72d1-8ed9-f9a34a01e327`)
- Reviewed: `6eae411` / `b120183`; fix `c7f5364`; final `e0440cb`
- Findings: 1 Major (native proof helpers did not pin `CPU_DISK`, so a clean run hit CUDA `cuInit` failures) + 2 Minor (3D default/no-op compared a stale 441-point surface; `requested` metadata was default-expanded).
- Fixes: both helpers call `process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)`; both 3D no-op cases run in fresh subprocesses and assert topology/counts; metadata renamed to `normalizedInput` + `inputSource` with the contract updated.
- Final: **no remaining Blocker/Major/Minor**; 24/24 new tests pass.
- Final full suite on main: **318 tests OK, 5 skipped**.

## Archive

- Tag: `archive/engine-complete-20261030` (points at the commit containing this log).
- Tarball: `/root/archives/viennaps-workbench-engine-complete-20261030.tar.gz`, built from the tag and filtered to exclude `var/`, `frontend/dist/`, `.venv/`, `runtime-patched/`, `docs/*-evidence/` and untracked acceptance directories.
- `main` and the tag are pushed to `origin` without force.

## Remaining

- Batch A4: particle shape distributions — branch `task/engine-particles` (`afe9f46`) intentionally not merged; masked Box/Sphere and CustomSphere are native-crash/inert blockers.
- 22 nm FinFET geometry-only flow (wafer start to M0).
