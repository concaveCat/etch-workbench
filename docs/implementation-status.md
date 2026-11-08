# Implementation and acceptance status

Updated 2026-10-26. This matrix tracks the formal PLAN.md A01–A15 criteria;
passing the automated test suite does not mean every browser or host acceptance
criterion has passed.

## Completed source and local verification

- P0 engine, geometry, material, CPU_DISK, and VTK evidence is retained in
  `var/probes/` and summarized in `capabilities.json`, `geometry-contract.md`,
  `resource-policy.json`, and `acceptance-spec.md`.
- Frozen project schema and API contract: `schema.json` and `api-contract.md`.
- Frontend dependencies are locked. `npm ci --offline`, `npm run typecheck`,
  and `npm run build` passed; npm reported zero vulnerabilities.
- Python dependency closure is pinned in `requirements.lock`, excluding
  ViennaPS/ViennaLS. FastAPI 0.162.0, Pydantic 2.14.1, and Uvicorn 0.54.0 are
  installed in `.venv`; `pip check` passes.
- Python unit and API integration suite passed **19/19 tests** under the bounded
  host runner with `WORKBENCH_GPU_TEST=1`, including the verified GPU path. The
  API tests use an in-process ASGI harness because the sandbox cannot bind a
  local socket; worker processes are individually supervised.
- Covered API/worker behavior includes project CRUD and validation, Host/Origin
  rejection, request size limit, real preview at grid deltas 0.1 and 0.05,
  isotropic etch/deposition displacement, two SiO2 deposits followed by etch,
  zero-rate no-op snapshots, CPU_DISK random replay and mesh comparison, GPU_DISK
  run/replay, single-run concurrency and separate-process instance lock, streamed
  ZIP output, injected disk-preflight failure, worker exception, cancel/reap,
  normal shutdown reaping, and field-path validation.
- Bounded host GPU verification reports one NVIDIA GeForce RTX 3060 Laptop GPU;
  `gpuAvailable()` returned true. `GPU_DISK` completed 2D single-particle etch
  and deposition, and the API supervisor/worker successfully ran and replayed a
  GPU_DISK project. `AUTO` remains CPU_DISK; other GPU flux engines remain
  unavailable. Evidence: `var/probes/gpu-engine-host.record.json`,
  `var/probe-tests/gpu-engine-results.json`, and
  `var/probes/gpu-api-integration.log`.
- The planned localhost server lifecycle now passes outside the sandbox: start,
  status, stop, and restart all succeeded with the app's saved PID/start-tick
  identity checks. `http://127.0.0.1:8766/`, both built assets, `/api/health`,
  and `/api/capabilities` returned HTTP 200. The service is left running for
  local use. Evidence: `var/probes/live-service-smoke.json` and
  `var/server.log`.
- A03 was additionally exercised with API previews for plane, reflective and
  periodic trenches, a 10° tapered trench with 0.5 µm mask and 5° mask taper, and
  the known incomplete trench. Geometry readbacks met the 2-grid tolerance and
  the incomplete contour failed. Evidence runs are `ddeca9fb34cb3cd1d3426dec`,
  `8d541c816e3b28278a05b2b4`, `2808ea32cb993d04a42fe3fd`,
  `f4a21be193c7b5761ecf9670`, and `7b5bca0a3b56d24719f82530`.
- A05 step reordering created a distinct successful run and requested hash:
  `6215c731b4a0f3f5ffd0f081` versus `9d28a0238ef9a3ea14eee1ca`.
- The independent VTK reader validated all five VTK files in the multi-step
  downloaded run archive and compared each with its viewer JSON. Connectivity,
  materials, and topology match; the largest coordinate serialization delta was
  `2.5e-9 µm`. The fixtures and both retained ViennaPS sample VTK files also pass.
- Python compilation and JSON parsing passed in the preceding verification.

## Acceptance matrix

| ID | Status | Evidence / remaining work |
| --- | --- | --- |
| A01 | Partial | The application is available at `http://127.0.0.1:8766/`; UI assets and API are live. The full create/run/view/save/reopen/export flow still needs an interactive browser session. |
| A02 | Partial | Real preview workers read back 0.1 and 0.05 on separate Domains; manifest worker result and requested hashes differ, and both runs remain readable. The UI computes and displays “对应其他输入配置,” but that interaction has not been browser-checked. |
| A03 | Pass | API previews cover the plane, both x boundaries, 10° trench taper, 0.5 µm/5° mask, fixed axis map, read-back enums, measured geometry within 2 grid cells, and rejection of the known incomplete contour. |
| A04 | Pass | Direct P0 isotropic probes and worker API runs agree on the signed rate×duration displacement within 2 grid cells for etch and deposition. |
| A05 | Pass | Real worker run `c4fe2890f12d3ec3276544cd` verifies two SiO2 deposits, subsequent etch, zero-rate no-op equivalence, and raw map `[10,30,30]`. Snapshot JSON and VTK now expose only the physical SiO2 boundary after adjacent same-material deposits; the central SiO2 boundary is at h=0.2 µm. The 19-test bounded suite passes. Reordering produced a second successful run with a different requested hash. |
| A06 | Pass | Direct ViennaPS calls and worker results match for isotropic etch/deposition (symmetric mesh distance 0) and CPU_DISK particle etch (distance 0). For the particle run, installed versions, grid, threads, engine, seed, rate, duration, time-step ratio, ray count, random-seed setting, sticking probability, and source exponent all match the execution record. |
| A07 | Partial | API exports and accepts a random CPU_DISK replay; same-material-set and symmetric mesh-distance checks pass, and an unsupported ViennaPS version is rejected. Browser file import remains unverified. |
| A08 | Pass | CPU and AUTO select CPU_DISK; explicit GPU selects GPU_DISK and succeeds for 2D single-particle etch/deposition. API replay retains GPU_DISK and succeeds. Isotropic steps with GPU preference record not-applicable. No fallback occurs. Schema-7 `solver.gpuEngine=GPU_TRIANGLE` is exposed only for the measured 2D/3D specialized sf6o2 etch on a reflective stack; other combinations return 422 without redirecting to GPU_DISK/CPU_DISK. GPU_LINE remains unavailable. |
| A09 | Pass | In-process API health/status/cancel calls complete below 2 seconds; cancellation reaches `cancelled` within 5 seconds with a persisted child record and empty owned group after reap. Process identity checks are used before signaling. |
| A10 | Partial | Bounded success, timeout, RSS, log-cap, worker exception, disk-preflight failure, cancel/reap, normal shutdown, and parent-death fixtures pass. An explicit finish/cancel race fixture remains. |
| A11 | Partial | API tests cover concurrent 409, a separate-process instance-lock attempt, empty-simulate rejection, Host/Origin checks, oversized bodies, unknown fields/schema/model, invalid units, bool-as-number, step and ray-resource limits. Browser file-import validation and remaining resource/geometry combinations remain. |
| A12 | Partial | Equal-scale SVG, shared comparison bounds, pan/zoom, and decimated grid labels are implemented. Resize and compare behavior have not been exercised in a browser. |
| A13 | Partial | API project create/load/update and immutable run inputs are implemented; browser JSON roundtrip and restart recovery have not been exercised. |
| A14 | Pass | The independent reader validated all five VTK files in a downloaded multi-step run, including finite coordinates, valid connectivity, aligned material IDs, resolved material map, and viewer-JSON topology. Maximum coordinate serialization delta was `2.5e-9 µm`. |
| A15 | Partial | Frontend typecheck/build, Python integration, bounded-process tests, live asset/API checks, and actual server start/stop pass. The browser flow remains unverified. The live local URL is `http://127.0.0.1:8766/`. |

## Environment limits

- The live service binds only to `127.0.0.1:8766` and is managed by the
  workbench's identity-checked scripts. No browser automation runtime is
  available in this execution environment, so interactive browser acceptance
  remains open.
- The sandbox itself still cannot initialize CUDA. The authorized host probe
  verified only GPU_DISK on the RTX 3060 Laptop GPU for 2D single-particle
  etch/deposition. Other GPU flux engines remain disabled; `AUTO` uses CPU_DISK.
- The read-only host self-check returned a non-zero status: static checks passed
  and runtime checks were skipped due to sandbox isolation. No system service,
  license, network interface, route, MAC, hostname, or `loopback0` state was
  changed.
