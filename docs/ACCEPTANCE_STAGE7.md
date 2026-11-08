# Stage 7 acceptance: MultiParticleProcess

## Result

Stage 7 passed on the isolated candidate using ViennaPS 4.6.2 and ViennaLS 5.8.5. The browser test submitted the project through the WebUI, ran the native process and `Process.calculateFlux()`, and compared returned coordinates, topology, material arrays, material map, and flux fields with separate direct ViennaPS reference calls. It also checked the native execution record, including species order, ray settings, seed, CPU thread count, callback invocation and etch/deposition signs.

## Implementation

- Added ion and neutral particle configuration, including the installed ion parameters, default and per-material neutral sticking probabilities, and per-material/species rate coefficients.
- The worker constructs `viennaps.MultiParticleProcess`, adds configured particle types, and passes the controlled coefficient table as its native rate callback. Etching returns negative velocity and deposition returns positive velocity. A fully zero coefficient table is recorded as a no-op without constructing or running a particle process.
- Added selected-step flux calculation through native `Process.calculateFlux()`. The stored JSON and VTP retain the native mesh and fields, field names, point/cell association, and execution metadata.
- Extended schema 7; formats 1–6 remain supported. Particle seed, thread count, engine, process parameters and ray settings are captured in run history and replay requirements.
- GPU and periodic combinations remain unavailable for the new process. Only directly compared CPU_DISK reflective cases are enabled.

## Verification

- Real Chromium acceptance: `frontend/tests/stage7-live.mjs`; report `docs/stage7-evidence/browser/browser-live.json`.
- Exact direct-reference comparisons passed for 2D process, 3D process, 2D replay, 2D flux and 3D flux. The 2D recipe included etch, deposition and a zero-rate no-op. The browser also saved/reopened a project, exported the project and run ZIP, and reloaded history. Browser page errors, console errors and failed requests were empty.
- Direct native fixtures, all through `scripts/bounded_child.py`: `tests/fixtures/stage7_multiparticle_reference.py` and `tests/fixtures/stage7_multiparticle_flux_reference.py`; captured inputs, outputs, logs and guard records are in `docs/stage7-evidence/browser/{2d,3d}/`.
- Full Python regression: **51 passed, 1 skipped**. The skipped test requires the explicit host GPU_DISK capability probe; this is deferred to Stage 9. Report: `docs/stage7-evidence/full-python-regression.json`.
- Frontend TypeScript/Vite production build passed. Vite reported the existing large JavaScript chunk warning. Browser test syntax check passed.

## Limits and review points

- The installed binding does not declare units for several ion angular/energy inputs or for native particle flux. The UI sends the entered numbers unchanged and states this explicitly; the table coefficients perform the conversion to the workbench velocity convention.
- The browser acceptance uses a compact built-in stack and validates CPU_DISK, reflective-boundary runs. GPU, periodic particle runs, and other material/model combinations have not been claimed as verified.
- `docs/stage7-evidence/browser/browser-live.json` captures the submitted projects and returned fields. Direct reference JSON, logs and `bounded_child.py` records allow replay and inspection.
