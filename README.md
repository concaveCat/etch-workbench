# Etch Workbench

**A plugin-first, engine-agnostic etch and topography simulation workbench.**

Etch Workbench builds 2D and 3D semiconductor structures, composes multi-step
etch/deposition recipes, runs them on CPU or NVIDIA GPU flux engines, and keeps
every step replayable. Process models and compute engines are both plugins:
a process plugin describes *what* to simulate, an engine plugin provides *how*
to compute it, and the platform owns state, replay, and visualization.

ViennaPS is the reference engine plugin. It is not hard-coded into the worker:
ViennaPS and a custom engine are loaded through the same `engine.toml`,
`engines.lock`, provider, lease, state-backend, and surface-backend contract.

Single-user and loopback-only by design: the service binds `127.0.0.1`, keeps
all data on local disk, and never calls an external API. Verified against
**ViennaPS 4.7.0 / ViennaLS 5.10.0** on Linux; the ViennaPS 4.6.2 / ViennaLS
5.8.5 runtime is retained for replay and capabilities the 4.7.0 binding does
not provide.

## Contents

- [Highlights](#highlights)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Engine plugins](#engine-plugins)
- [Process plugins](#process-plugins)
- [Replay and state](#replay-and-state)
- [HTTP API](#http-api)
- [Testing](#testing)
- [Known limitations](#known-limitations)
- [Repository layout](#repository-layout)
- [License](#license)

## Highlights

**Structures and recipes**

- 2D plane/trench and 3D plane/trench/hole/fin/stack/GDS structures.
- 15 built-in materials plus user-defined materials, with independent film and
  mask roles.
- Micrometre and nanometre project units.
- Isotropic etch/deposition; ray-traced single-particle etch/deposition;
  SF6/O2, CF4/O2, HBr/O2, SF6/C4F8, fluorocarbon, ion-beam, Faraday-cage,
  wet etch, TEOS, ALD, oxidation, oxide regrowth, selective epitaxy, neutral
  transport, and multi-particle models.
- GDS mask import, planarize, boolean, particle-shape, insert/remove geometry
  steps, per-step advection overrides, and per-step native parameters.

**Execution and inspection**

- One active run at a time in a supervised child process with fixed seed,
  timeout, RSS watch, cancellation, and replayable inputs.
- CPU and GPU flux engines, explicit GPU device pinning, and no silent fallback.
- 2D/3D snapshot viewer with material visibility, clipping planes, sections,
  camera persistence, and scale bar.
- Surface flux maps, native intermediate outputs, CSV/PNG export, artifact ZIPs.
- Per-step `state.json`, `outputs.json`, `plan.json`, `geometry.vpsd`, and
  `fields.npz` for exact replay.
- Replay-project export/import and continuation packages.

**Plugin architecture**

- Process plugins are installable units: `plugin.toml` + `ProcessSpec` +
  `VariantRegistration` + `plugins.lock`.
- Engine plugins are installable units: `engine.toml` + `EngineProvider` +
  `engines.lock`.
- A process step can be submitted as `model="plugin"` with a `pluginId` and
  params, without changing the project schema for every new process.
- A project can select an installed engine with `solver.engineId`,
  `solver.engineKernel`, and `solver.engineBackend`.
- `GET /api/engines` lists installed engine capabilities.
- Custom engines can expose an engine-neutral `SurfaceBackend` for preview
  without teaching the worker their native handle type.

## Architecture

```text
Browser (React + TypeScript + vtk.js)
   │  HTTP/JSON on 127.0.0.1 only
   ▼
FastAPI service  backend/main.py
   │  in-process supervisor, one active run, identity-checked lifecycle
   ▼
worker subprocess  worker/simulate.py
   │
   ├─ ProcessRegistry       process plugins (plugin.toml + plugins.lock)
   ├─ WorkerV2Session       one run-scoped FlowSession
   │    ├─ EngineRegistry   engine plugins (engine.toml + engines.lock)
   │    │    ├─ ViennaPS engine plugin
   │    │    └─ custom engine plugin
   │    └─ StateStore       per-step state/outputs/plan/geometry
   ▼
var/runs/<run-id>/           input.json, manifest.json, snapshots, flux, artifacts
```

The web process only imports package metadata and `vtk.js` files. Native engine
libraries are loaded exclusively inside the worker child. Run outputs are plain
files, so a run can be inspected or replayed without the original process.

| Path | Role |
| --- | --- |
| `backend/` | FastAPI app, validation, storage, supervisor, native bindings |
| `worker/` | Simulation entry point, step executors, profiling helper |
| `process_models/` | Engine/process contracts, executor, state store, dispatch |
| `engine_plugins/` | Installed engine plugins; `viennaps_engine/` is the reference |
| `plugins/` | Installed process plugins |
| `frontend/` | React + TypeScript UI and vtk.js viewer |
| `scripts/` | Start/stop, build, lock, host probes, acceptance helpers |
| `tests/` | Python unit and API integration tests |
| `docs/` | Contracts, schemas, acceptance reports, capability matrices |
| `var/` | Runtime data: projects, runs, domain states, GDS assets (git-ignored) |

## Requirements

| Component | Requirement |
| --- | --- |
| OS | Linux (uses `/proc` for process supervision and CPU affinity) |
| Python | 3.12 |
| ViennaPS | 4.7.0 (default, `requirements-v470.lock`); 4.6.2 retained for replay/legacy. A CUDA build is required for GPU runs |
| ViennaLS | 5.10.0 (default); 5.8.5 for the retained 4.6.2 runtime |
| Node.js | 20.19+ or 22.12+ (Vite 8) |
| GPU | Optional. NVIDIA GPU with CUDA for `GPU_DISK` |

ViennaPS and ViennaLS are deliberately **not** application dependencies: the
workbench must use the engine build already installed on the host and must never
upgrade or replace it. The virtual environment is therefore created with
`--system-site-packages`.

## Installation

```bash
git clone https://github.com/concaveCat/etch-workbench.git
cd etch-workbench

# 1. Python environment that can see the system ViennaPS/ViennaLS.
python3.12 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.lock

# 2. Frontend bundle (the start script refuses to run without it).
cd frontend
npm ci
npm run build
cd ..
```

Check that the engines are visible to the venv:

```bash
.venv/bin/python -c "import viennaps, viennals; print(viennaps.__version__, viennals.__version__)"
# expected: 4.7.0 5.10.0   (the retained legacy venv prints 4.6.2 5.8.5)
```

If the import fails, install ViennaPS/ViennaLS first (including their Python
bindings) and re-create `.venv` with `--system-site-packages`.

## Quick start

```bash
./scripts/start.sh     # starts the service in the background
./scripts/status.sh    # prints the URL and health state
./scripts/stop.sh      # stops only the process it started
```

Then open the printed address, for example <http://127.0.0.1:8789/>.

The port is chosen in this order:

1. `WORKBENCH_PORT` environment variable,
2. `var/listen-port.json` (written when the service is moved between ports),
3. built-in default `8767`.

The service always binds `127.0.0.1`; requests whose `Host` header does not match
the active loopback address and port are rejected. Logs go to `var/server.log`.

## Engine plugins

An engine plugin is an installable unit:

```text
engine_plugins/
  viennaps_engine/
    engine.toml
    plugin.py
  my_engine/
    engine.toml
    plugin.py
```

`engine.toml`:

```toml
api = 1
engine_id = "myengine"
version = "1.0.0"
entrypoint = "my_engine.plugin:PLUGIN"
description = "My custom compute engine"
```

The entrypoint exposes an `EnginePlugin`:

```python
class EnginePlugin(Protocol):
    engine_id: str
    version: str
    def create_provider(self, context: EngineContext) -> EngineProvider: ...
```

The engine implements the same contracts as ViennaPS:

- `EngineProvider.engines()` / `acquire()`
- `EngineLease` with `handle`, `profile`, `fingerprint`, `state_backend`,
  optional `cellset_backend`, optional `surface_backend`, and `release()`
- `StateBackend.checkpoint()` / `write()` / `load()` / `validate()`
- optional `SurfaceBackend.extract(state) -> SurfaceData` for the viewer

After adding or changing an engine plugin:

```bash
.venv/bin/python scripts/lock-engines.py
.venv/bin/python scripts/lock-engines.py --check
```

`engines.lock` pins the manifest bytes and implementation bytes. A missing or
drifting plugin fails at load time; there is no fallback to another provider.

## Process plugins

A process plugin is also an installable unit:

```text
plugins/
  my_process/
    plugin.toml
    params.py
    step.py
    spec.py
    variants/native.py
    register.py
```

`plugin.toml`:

```toml
api = 1
id = "my_process"
version = "1.0.0"
entrypoint = "my_process:register"
description = "My process model"
```

After adding or changing a process plugin:

```bash
.venv/bin/python scripts/lock-plugins.py
.venv/bin/python scripts/lock-plugins.py --check
```

A project step can address an installed process plugin without a schema change:

```json
{
  "id": "plugin_step_01",
  "name": "my process",
  "enabled": true,
  "model": "plugin",
  "pluginId": "my_process",
  "params": {
    "operation": "etch",
    "rateMagnitude": 0.1,
    "duration": 1.0
  }
}
```

The v2 dispatcher expands this envelope to `model=my_process` for the plugin's
`parse_step` hook. `model="plugin"` always runs through the registry path, even
when legacy native steps remain on the opt-in default.

### Selecting an engine

Schema-7 projects can select an installed engine:

```json
"solver": {
  "engineId": "myengine",
  "engineKernel": "MY_KERNEL",
  "engineBackend": "cpu"
}
```

`engineId` and `engineKernel` must be provided together. `engineId` is mutually
exclusive with native `gpuEngine`. An uninstalled engine is rejected during
project validation, not silently replaced.

## Replay and state

Each step keeps its own replay artifact:

```text
<run_dir>/v2/<step_id>/
  state.json        # materials, geometry ref, layers, fields metadata
  outputs.json      # metrology / artifacts
  plan.json         # process / variant / engine / profile identity
  geometry.vpsd     # native geometry, when the engine can write it
  fields.npz        # cell-set arrays at field boundaries
  flow.json         # one-step flow record
```

`WorkerV2Session` loads the process registry, engine registry, provider, and
state backends once per run; `FlowSession` still executes and persists one step
at a time. That preserves per-step snapshots, GPU context teardown boundaries,
and exact replay.

## HTTP API

All endpoints are relative to the loopback base URL and exchange JSON. Errors use
`{"detail": ...}` with `4xx` status codes. The interactive OpenAPI routes are
disabled on purpose; `docs/api-contract.md` is the normative contract.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Service, Python and engine versions, active run |
| `GET` | `/api/capabilities` | Verified capabilities and resource policy |
| `GET` | `/api/engines` | Installed engine plugins and capabilities |
| `GET` | `/api/gpu-devices` | Visible CUDA devices and UUIDs |
| `GET` | `/api/materials` | Built-in and custom material catalog |
| `POST` | `/api/gds-assets` | Upload a GDS layout (≤ 64 MiB) |
| `POST` | `/api/projects/validate` | Validate a project without saving |
| `GET`/`POST` | `/api/projects` | List or create projects |
| `GET`/`PUT` | `/api/projects/{id}` | Read or replace a project |
| `POST` | `/api/runs` | Submit a simulation run (202) |
| `POST` | `/api/runs/flux` | Single flux calculation for one step |
| `POST` | `/api/runs/slice` | Surface slice extraction |
| `POST` | `/api/runs/replay` | Replay an exported replay project |
| `GET` | `/api/runs/{id}` | Run manifest and step status |
| `GET` | `/api/runs/{id}/snapshots/{step}` | Snapshot mesh as JSON |
| `GET` | `/api/runs/{id}/download` | Streamed ZIP of the run |
| `GET` | `/api/runs/{id}/replay-project` | Export a replay envelope |

## Testing

```bash
# Full Python suite; use an isolated data directory.
VIENNAPS_WORKBENCH_DATA_DIR=$PWD/var/test-$(date +%s) WORKBENCH_PORT=8767 \
  .venv/bin/python -m unittest discover -s tests -t . -v

# Targeted plugin / v2 / engine suites
.venv/bin/python -m unittest tests.test_engine_plugins tests.test_v2_surface -v
.venv/bin/python -m unittest discover -s tests -p 'test_plugin_*.py'

# Style, typecheck, lock checks
PYTHON=.venv/bin/python scripts/check-style.sh
.venv/bin/python scripts/lock-engines.py --check
.venv/bin/python scripts/lock-plugins.py --check
```

## Known limitations

- Native steps still default to the legacy execution path; set
  `VIENNAPS_WORKBENCH_V2_EXECUTION=1` to use the v2 registry for native steps.
  `model="plugin"` steps always use the v2 registry.
- ViennaPS is the only installed reference engine plugin. A custom engine can
  be added in-process; a subprocess/remote engine adapter is not included.
- ViennaPS native helpers, geometry operations, and cell-set internals still
  live in shared modules and are scheduled for a later engine-plugin extraction.
- GPU support is limited to `GPU_DISK` for covered combinations; GPU_TRIANGLE is
  exposed only for archived evidence rows. Multi-ion GPU runs require the
  patched ViennaPS 4.6.2 runtime.
- The UI is a single-user local tool: no authentication, no TLS, no remote
  access.
- Browser acceptance scripts assume the host toolchain and are not hermetic.

## Repository layout

```text
backend/          FastAPI app, models, validation, storage, supervisor
worker/           simulate.py, step executors, profiling helper
process_models/   process/engine contracts, executor, state store, dispatch
engine_plugins/   engine.toml plugins; viennaps_engine is the reference
plugins/          process plugins; plugin.toml + implementations
frontend/         React/TypeScript UI, vtk.js viewer, benchmark scripts
scripts/          start/stop/status, build, lock, host probes
tests/            Python unittest suite and fixtures
docs/             contracts, schemas, capability matrices, acceptance reports
var/              runtime data (ignored by git)
```

## Documentation

- `docs/engine-plugins.md` — engine.toml, engines.lock, provider injection,
  custom engine surface backend.
- `docs/v2-session-dataflow.md` — run-scoped FlowSession, per-step replay,
  registry/provider reuse.
- `docs/plugin-architecture.md` — process plugin layers, state, and execution.
- `docs/api-contract.md` — wire contract, hashes, replay envelope.
- `docs/schema-project-*.json`, `docs/schema-replay-*.json` — project and replay
  schemas.
- `docs/capabilities.json`, `docs/resource-policy.json` — verified behavior and
  limits.

## License

No license has been chosen yet. Until one is added, the source is
all-rights-reserved by default. If you plan to reuse it, open an issue to discuss
a license.

ViennaPS is a separate GPL-3.0 project from TU Wien. This workbench is not
affiliated with or endorsed by TU Wien.
