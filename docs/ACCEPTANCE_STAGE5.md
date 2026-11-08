# Stage 5 acceptance: free geometry and GDS

Status: implemented and passed the automated acceptance available for the installed ViennaPS 4.6.2 / ViennaLS 5.8.5 environment. The formal baseline at `/root/viennaps-workbench-selectivity` and its service/data on port 8769 were not modified.

## Implementation

- Extended the unified schema 7 project model with Fin construction, GDS-backed geometry, extrusion, and true slice recipe entries. Schemas 1–6 and their existing replay paths remain accepted.
- Fin generation calls `viennaps.d2.MakeFin` in 2D. For 3D it builds the 2D native Fin and uses `viennaps.Extrude` along axis 1 into a native 3D Domain.
- GDS upload is stored by SHA-256. The interface can inspect structure/layer metadata, set units, position, layer heights, and per-layer material, then preview the native generated geometry. Project and run ZIPs include the GDS bytes and validate the digest on import.
- ViennaPS 4.6.2's installed binding does not expose a structure selector on `GDSGeometry.layerToLevelSet`. The adapter in `backend/gds.py` selects the requested structure plus its reference dependency closure and requested layers. Unselected shapes are assigned to an unused hidden support layer so they contribute only to the native layout-wide bounds; only requested selected-cell/layer output is converted into the simulation Domain. The UI labels the bounds and explains this behavior.
- True slicing calls `viennaps.Slice(domain, sliced_domain, 1, position)` (the y-normal plane in the Workbench 3D coordinate convention) and saves a separate native 2D result. It is recorded as `viewerClipping: false`; replay uses the slice analysis mode. This is distinct from viewer clipping.

## Automated acceptance

- `tests.test_stage5` runs API/UI-facing requests through the actual service/supervisor/`bounded_child.py` worker and compares results with independent direct ViennaPS fixtures. Exact coordinates, topology, cell material IDs, level-set material mapping, bounds, and true slice output are checked.
- Coverage includes Fin in 2D and 3D extrusion; GDS in 2D and 3D; selected structure/layer/material/position inputs; hierarchical reference closure with an unrelated same-layer cell excluded; true 3D slicing and replay; project save/reopen; portable package export/import after removal of the stored source asset; run ZIP; history and replay.
- Full regression `WORKBENCH_ACCEPTANCE_STAGE=stage5 ./.venv/bin/python scripts/run_acceptance.py`: 46 passed, 1 skipped. The skipped case is host GPU_DISK capability verification, deferred to Stage 9.
- Production Vite build and browser script syntax check passed. Real Chromium e2e against the isolated candidate service on `127.0.0.1:8780` passed with zero page errors, console errors, or failed requests. It created 2D/3D GDS runs, a true slice, and a slice replay; direct-native mesh signatures matched exactly.

## Evidence

- Full Python regression: `docs/stage5-evidence/full-python-regression.json`.
- Browser requests, run IDs, direct-mesh match flags and browser errors: `docs/stage5-evidence/browser-live.json` (GDS 2D `80a974787b6f580ff51d6714`, GDS 3D `3f626347db9c1fcd4d245c37`, slice `522507082eaba5e089cc6d10`, replay `13688112b1c40dd2501600a4`).
- Independent direct GDS and Fin comparisons, bounded-child guards/logs, and focused reports: `docs/stage5-evidence/gds-acceptance.json`, `fin-slice-acceptance.json`, `hierarchical-gds-acceptance.json`, and adjacent `direct-*` directories.
- Portable project and slice run archives: `docs/stage5-evidence/browser-gds-project.zip`, `browser-gds-slice-run.zip`; browser replay: `browser-gds-slice-replay.json`.

## Limits and independent-review points

- The native binding has no direct GDS structure selector and an isolated-cell 3D conversion can fail when the native layout-wide bounding box is empty. The hidden support-layer workaround is tested for selected and unrelated same-layer cells, but should receive focused review.
- ViennaPS output and Workbench coordinates use micrometers; the GDS unit conversion is explicit and recorded in inspection metadata. The installed binding's structure-selection gap is not hidden from users.
- One GPU_DISK host-capability test remains skipped until the Stage 9 GPU audit. The existing Vite bundle-size warning remains.

Stage 5 completion does not imply Stages 6–9 are complete.
