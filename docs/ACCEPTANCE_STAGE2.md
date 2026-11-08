# Stage 2: recipe geometry operations

Status: candidate implementation and automated acceptance passed; ready for independent review. The formal baseline at `/root/viennaps-workbench-selectivity` and service on port 8769 were not modified.

## Implemented

- Schema 5 now accepts `removeMaterial`, `planarize` and `boolean` recipe steps. Geometry steps have no process rate, duration, particle, seed or flux fields. Formats 1–4 remain accepted and their existing fixtures passed regression.
- The recipe editor adds all three operations. Each supports enable/disable, copy, reorder and deletion. Geometry controls show their native parameters and never show a made-up process rate or physical time.
- The worker applies each enabled operation to the same ViennaPS `Domain` produced by the previous step, then records and snapshots that actual domain. It calls `Domain.removeMaterial(material)`, `Planarize(domain, cutoffHeight).apply()`, and `Domain.applyBooleanOperation(boxStencil, operation, applyToAll)`.
- Boolean operands use the installed `GeometryFactory.makeBoxStencil` binding. 2D position is `[x,h,0]`; 3D is `[x,y,z]`, with z vertical. Width runs along x; height is vertical; the 3D depth is the y extrusion length. Native operation mapping is `union → UNION`, `intersection → INTERSECT`, `difference → RELATIVE_COMPLEMENT`.
- History records the geometry operation and exact native parameters with engine `not-applicable`, without rate or duration. Save/open, project JSON export/import, result ZIP, history load and replay all preserve the recipe.

## Automated acceptance

- `tests.test_geometry_operations` compares real API runs with `tests/fixtures/geometry_operations_direct_reference.py`, which calls ViennaPS directly inside `scripts/bounded_child.py`. It checks every intermediate 2D and 3D surface's points, connectivity, cell material IDs and level-set material map. Maximum coordinate error was `0`; tolerance was `1e-10`.
- The compared recipe interleaves W deposition, planarization, all three Boolean operations, material removal and a disabled geometry step. Replay reproduced every compared mesh exactly. Project save/reopen and run ZIP/replay exports were checked.
- `frontend/tests/geometry-live.mjs` used real Chromium against candidate port 8779, with no API mocks. It entered parameters in the WebUI, copied/reordered/disabled a step, ran both 2D and 3D, and compared browser-submitted configurations to direct ViennaPS results. A nonzero 2D h origin of `0.1` mapped to 3D z origin `0.1`; 3D `applyToAll=false` also reached the direct reference unchanged. Browser page errors: none.
- The browser flow also exercised project save/open/export/import, result ZIP, history display and replay. Current browser run IDs: 2D `c284c6f726ddfa268d11b647`, replay `03bb25a0e3d2dd0e280f504f`; 3D `67471aeed79981167762292e`.
- Full Python regression: 38 tests passed, 1 host GPU_DISK test skipped because that host capability was not verified. TypeScript check and Vite production build passed. The only build note is the existing large JavaScript bundle warning.

## Evidence

- Browser inputs, actual execution records, reference meshes, comparison manifests and bounded-child guards: `docs/stage2-evidence/browser-geometry-*`.
- 2D and 3D API/direct-reference comparisons: `docs/stage2-evidence/geometry-operations-{2d,3d}-comparison.json`.
- Installed-binding probe: `docs/stage2-evidence/native-geometry-probe.json` and its log/guard.
- Regression output: `docs/stage2-evidence/regression.log`.

## Scope limits

- Boolean geometry currently offers a native box stencil. Other primitives and GDS construction belong to Stage 5.
- `INVERT` is not offered: the installed 4.6.2/5.8.5 probe produced the same reported mesh summary as the input when calling `Domain.applyBooleanOperation` with `INVERT`. Expose it only after a separate native behavior check explains the binding semantics.
- Native operations use ViennaPS material and geometry identities already present in this build. User-defined materials remain Stage 6.
