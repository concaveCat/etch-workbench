# Stage 3: native Domain persistence and continuation

Status: implemented and passed automated API, native-reference, and real Chromium acceptance. The formal baseline at `/root/viennaps-workbench-selectivity` and its service on port 8769 were not modified.

## Implementation

- Added schema 6 for continuation projects while preserving formats 1–5. Schema 6 is the planned extension point for later roadmap capabilities; stages should extend it rather than incrementing the project version for each new model.
- The bounded ViennaPS worker uses native `viennaps.Writer(domain, path).apply()` to save a `.vpsd` at the initial Domain and after each completed process, geometry, and no-op step. The actual bytes are SHA-256 addressed; manifest/history metadata records the file name, dimension, mesh setup, boundaries, level-set material map, level-set count, structure fingerprint, and ViennaPS/ViennaLS versions.
- Continuation uses a fresh compatible `Domain` and native `viennaps.Reader(domain, path).apply()`. The worker rejects mismatched state hashes, engine versions, grid delta, level-set count/material mapping, or actual boundary conditions. The project model validates dimension, grid spacing, x/y extents, boundary mode, and the canonical initial geometry fingerprint.
- `runMode` and replay envelopes distinguish `from-initial` from `from-domain-state`. Projects use content-addressed state IDs without absolute paths. Project/state ZIPs contain `project.json` and `domain-state.vpsd`; state-backed replay packages add `replay-project.json`. Import rejects unexpected/duplicate entries, symlinks, encrypted entries, excessive sizes, incompatible engine versions, and state hash mismatches.
- The result history adds “以此状态开始新工艺” for snapshots that have a saved native state. The UI labels the run mode, preserves the state when adding new recipe steps, disables dimension conversion while bound to a state, and offers an explicit action to clear the state and run from the initial structure.
- Added `VIENNAPS_WORKBENCH_DATA_DIR` so candidate browser and regression services can use a separate data directory while remaining managed by the existing start/status/stop scripts. The default service data directory remains `<candidate>/var`.

## Automated acceptance

- `worker/probe_domain_persistence.py` was executed under `scripts/bounded_child.py` using ViennaPS 4.6.2 / ViennaLS 5.8.5. Native Writer/Reader round trips preserved exact surface points, cells, cell MaterialIds, level-set material maps, boundary labels, grid delta, bounding box, and level-set count in 2D and 3D.
- `tests.test_domain_continuation` compares the API continuation against an independent direct ViennaPS script which writes and reads its own `.vpsd`, then compares it with an uninterrupted direct process sequence. The resumed and uninterrupted surfaces matched exactly in coordinates, topology, cell material IDs, material maps, and grid spacing in both dimensions. Replay from the saved state matched as well.
- API acceptance covered saving/opening a continuation project, portable package export/import, state hash rejection, replay package export/import, run ZIP inclusion of input and snapshot `.vpsd` files, recorded versions/boundaries/material maps, and legacy schema preservation.
- `frontend/tests/domain-continuation-live.mjs` drove the actual WebUI in Chromium against candidate port 8779 for both 2D and 3D. It submitted the source run and continuation through the browser, created a continuation from history, saved/reopened the project, exported/imported project and replay packages, replayed from the state, and compared output meshes against the independent direct reference. No page errors occurred.
- Full automated regression: `scripts/run_acceptance.py` ran 40 tests; all passed except one GPU_DISK test skipped because the host capability probe is not verified. TypeScript and Vite production build passed; Vite reported the existing large-bundle warning.

## Evidence

- Installed Writer/Reader native round trip: `docs/stage3-evidence/writer-reader-probe.json`, `.log`, `.guard.json`, and the 2D/3D `.vpsd` files.
- API direct references and comparisons: `docs/stage3-evidence/continuation-acceptance.json`, `continuation-{2d,3d}-direct-reference.json`, `continuation-{2d,3d}-reference-guard.json`, and the isolated `workbench-data-*` directories.
- Real-browser evidence: `docs/stage3-evidence/browser-continuation-live.json`, per-dimension JSON, direct reference files, `.vpsd` files, package ZIPs, and bounded-child logs/guards.
- Regression summary and data path: `docs/stage3-evidence/full-python-regression.json`; automated command: `.venv/bin/python scripts/run_acceptance.py`.
- Final Chromium runs: 2D source/continuation/replay `e8b0e16225155c1a76e04abd` / `9abe143f2ec5a2124ec381dc` / `d8eeafceee1d43813cdf1b10`; 3D `1733833551b85deae69df76c` / `efecffe9a7c4898fa2bd18f5` / `f91d779065cb95afe5edcb1c`.

## Limits and review points

- Older historical runs created before Stage 3 have no native `.vpsd` snapshot. They remain viewable/replayable from their original recipe, but cannot be continued from a state that was never saved; the API reports this explicitly.
- The installed `.vpsd` file does not contain engine version metadata. The application stores and verifies ViennaPS/ViennaLS versions in the sidecar project/manifest reference before calling Reader.
- Native states and run artifacts share the configurable per-run cap (`WORKBENCH_MAX_ARTIFACT_MIB`, default 4 GiB) and the 10 GiB workbench cap. Larger states are rejected before or during artifact creation.
- GPU behavior is not expanded by this stage. The existing unverified-host GPU test remains skipped; all Stage 3 direct references use the verified CPU process path.
- Items for independent review: schema-6 portability/hash semantics; ZIP import bounds; continuation state compatibility checks; whether snapshotting every completed step is appropriate for the current artifact budget.

Stage 3 completion does not imply Stages 4–9 are complete.
