# Stage 6 acceptance: custom materials and native Domain outputs

## Delivered

- Added a persistent custom-material registry for ViennaPS 4.6.2. New names are registered in append-only order as native IDs 177–240 (`customId` 0–63). Names are case-insensitively unique against the complete installed built-in inventory. Identity cannot be renamed, deleted, or reused; color, density, conductivity, and description remain editable metadata.
- The bounded worker registers custom materials with `viennaps.MaterialRegistry.instance().registerMaterial()`, checks both native `customId()` and `legacyId()`, writes `MaterialInfo` through `getInfo()` / `setInfo()`, and checks the readback. Metadata does not imply an etch, deposition, or sticking law. The UI asks the user to configure process behavior separately.
- Extended schema 7 with optional output settings and custom material definitions. Existing project formats 1–6 and old hash inputs remain supported. Custom definitions are embedded in saved/exported projects and replay inputs; the worker registers the same stable prefix in every child process.
- The workbench always stores ViennaPS surface meshes used by the viewer. Optional per-snapshot outputs call `Domain.saveLevelSets()`, `Domain.saveLevelSetMesh()`, and, in 3D only, `Domain.saveVolumeMesh()`. Native `Domain.getMetaData()` values are stored in a JSON sidecar because the installed ViennaPS `Writer` / `Reader` roundtrip does not preserve metadata. Artifact links validate paths, size, and SHA-256 before serving; run ZIPs include validated outputs.
- Metadata sidecars preserve original names and values, dimension, grid spacing, material map, and native metadata level. ViennaPS does not expose units or point/cell association for Domain metadata, so these remain explicitly unspecified.

## Verification

- Targeted integration: `tests.test_stage6` passed. Its independent 2D and 3D references use `tests/fixtures/stage6_material_output_reference.py` through `scripts/bounded_child.py` and compare points, connectivity, per-cell material IDs, material map, native output inventory, and exact output-file bytes. It also covers stable IDs across child processes, metadata updates, duplicate rejection, save/reopen, replay, and run ZIP contents.
- Full regression: 47 passed, 1 skipped. Report: [full-python-regression.json](stage6-evidence/full-python-regression.json). The skipped test requires the host GPU_DISK capability probe and remains in Stage 9 validation.
- Real Chromium acceptance on isolated `127.0.0.1:8781`: [browser-live.json](stage6-evidence/browser-live.json). The page registered a custom material, submitted a 2D etch, selected level-set and metadata outputs, saved/reopened and exported/imported the project, downloaded a native artifact over HTTP, exported and replayed the run, loaded history, and downloaded the run ZIP. The browser-submitted initial/final meshes and native output bytes matched a separately run direct ViennaPS reference exactly. There were no page errors, console errors, or failed requests.
- Browser direct-reference guard and log: `stage6-evidence/browser-direct-reference/direct.guard.json` and `direct.log`; input project and custom registry are saved beside them. Python direct-reference evidence is in `stage6-evidence/custom-{2d,3d}-*`.
- TypeScript check and production Vite build passed. Existing Vite large-bundle advisory remains (about 1.19 MB minified).

## Review notes and limits

- The install's complete built-in material ID/name inventory is recorded in `docs/native-material-ids-4.6.2.json`; only the curated built-in catalog and registered custom materials are selectable.
- Custom registry persistence is per workbench data directory. Portable projects embed definitions; when imported into a different registry, stable IDs are reconciled and collision/identity checks run before execution.
- The tested 2D browser flow cannot request a 3D volume mesh; 3D volume output is verified by the direct-native 3D integration test.
- The host capability test is not part of Stage 6 acceptance. Stage 9 must verify CPU/GPU combinations and explicit GPU failure behavior.
