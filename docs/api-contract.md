# API and data contract, version 1

This document freezes the P1 wire contract before the browser client is integrated.
The JSON Schema in `schema.json` is normative for project files. API validation also
applies the operational checks in `resource-policy.json`. Error responses use paths
relative to the submitted JSON body.

## Project and replay files

An ordinary `project.json` has exactly the fields described by `schema.json`.
`schemaVersion` must be `1`, `dimension` must be `2`, and units are fixed to
`um`, `s`, `um/s`, and `deg`. Unknown fields and future schema versions are rejected.
The service assigns project IDs; imported IDs and file paths are ignored.

Step IDs are unique within one project and may not equal `initial`. Copying a step
creates a new ID; moving a step preserves its ID. Empty steps are valid for save,
load, validate, and preview. Simulate requires at least one enabled step. Disabled
steps remain in the saved project but are excluded from execution and the requested
input hash. Model-specific fields are required only by `singleParticle`; they are
rejected on `isotropic`. A deposit requires `depositMaterial: "SiO2"`; an etch may
not carry a deposit material.

The separate `replay-project.json` envelope is:

```json
{
  "format": "viennaps-replay",
  "schemaVersion": 1,
  "normalizedProject": {"schemaVersion": 1, "...": "project fields"},
  "executionRequirements": {
    "viennapsVersion": "4.6.2",
    "viennalsVersion": "5.8.5",
    "cpuThreads": 1,
    "steps": [{"stepId": "step_12345678", "engine": "CPU_DISK", "seed": 314159}]
  }
}
```

The envelope has no fixed byte-size limit and is validated before it is saved. The
normalized project has `random.mode=fixed`; its seed is the actual run seed. Every
enabled particle step has one requirements entry with the actual engine and seed.
The current implementation only accepts the recorded ViennaPS/ViennaLS versions,
the recorded thread count, and a verified engine combination. It rejects unsupported
requirements instead of silently selecting another engine or seed. `POST
/api/runs/replay` accepts this envelope and submits a replay run after these checks.
Particle requirements must list each enabled nonzero single-particle step exactly
once; v1 requires one shared seed and one concrete engine for those steps. The
verified host currently supports CPU_DISK and GPU_DISK; GPU_DISK replay is accepted
only where the host capability record exposes the verified combination.

`solver.cpuThreads` and replay `executionRequirements.cpuThreads` accept values from
1 through the process's available logical CPU count. `GET /api/capabilities` returns
that runtime maximum as `resourcePolicy.run.cpuThreads.maximum`; on Linux it follows
the process CPU affinity/cpuset, with `os.cpu_count()` as a fallback. The project
default remains one thread.

## Canonical hashes

The server applies type normalization, removes view fields, project name, all step
names and IDs, disabled steps, and `timeoutSeconds`. It keeps the physical domain
and geometry and enabled steps in their execution order. Solver settings are
included only when a nonzero-rate step uses them: `timeStepRatio` and `cpuThreads`
for process steps, and `raysPerPoint`, `random` intent, and `enginePreference` for
single-particle steps. A zero-rate step hashes only its operation, zero rate, and
duration because its model and model-specific settings are not executed. Fields not
applicable to the selected model are rejected, not hashed as latent settings. The
resulting UTF-8 JSON uses sorted object keys,
compact separators, and finite JSON numbers; SHA-256 lowercase hex is
`requestedInputHash`.

For example, these two inputs have the same requested hash if their physical fields
match: one may have a different project name, view, step names/IDs, or disabled-step
text. Reordering enabled steps, changing an enabled physical parameter, changing
applicable seed intent, thread count, or engine preference changes the hash. Random intent is
hashed as `{"mode":"random"}`; generated seeds do not change the requested hash.

`executionHash` is SHA-256 over sorted compact JSON containing
`requestedInputHash`, actual run seed (null if no particle step uses randomness), thread count, the concrete engine and seed
for each enabled particle step in order, and ViennaPS/ViennaLS versions. Isotropic
steps record their engine as `not-applicable` and have no random seed. Execution
metadata is finalized only after the worker reports installed versions and actual
parameters.

## API

All routes are same-origin JSON under `/api`; the server binds only to loopback.
Requests with a non-local Host or a cross-origin Origin are rejected. There is no
CORS allowance. JSON project, replay, and run request bodies have no fixed
byte-size cap. Run creation snapshots the normalized body into the run directory
before returning; later edits cannot change it. GDS uploads retain a 64 MiB cap,
and continuation packages retain the effective artifact/archive cap
(`WORKBENCH_MAX_ARTIFACT_MIB`, default 4096 MiB).

### Health and capabilities

`GET /api/health` returns service status and package metadata only; it does not
import ViennaPS or initialize CUDA.

```json
{"status":"ok","applicationVersion":"0.1.0","python":"3.12.14",
 "viennaps":"4.6.2","viennals":"5.8.5","activeRunId":null}
```

`GET /api/capabilities` returns the versioned `docs/capabilities.json` and
`docs/resource-policy.json` content. The resource policy is updated in place
with the effective runtime limits; an additive `runtimeLimits` object reports
`artifactLimitBytes`, `artifactLimitMiB`, the `WORKBENCH_*` environment
variables, defaults, minimums (`maxMiB: null` means no upper bound), and whether
each value came from `env` or `default`. This host has verified 2D single-particle
GPU_DISK etch and deposition. `auto` maps to CPU_DISK; explicit GPU preference maps
to GPU_DISK and never silently falls back. Other GPU flux engines are not exposed.

### Project validation and storage

- `POST /api/projects/validate` takes a project object and returns
  `{"valid":true,"warnings":[],"requestedInputHash":"..."}` or HTTP 422 with
  `{"detail":[{"path":"steps[0].duration","message":"..."}]}`.
- `GET /api/projects` returns `{"projects":[{"id":"...","name":"...",
  "updatedAt":"..."}]}`.
- `POST /api/projects` takes one project object and returns `{"id":"...",
  "project":{...}}` with HTTP 201.
- `GET /api/projects/{id}` returns the stored project. `PUT` takes a complete
  replacement project and returns the stored object. IDs are server-generated
  lowercase hex and are never interpreted as paths.

### Run lifecycle

`POST /api/runs` takes `{"kind":"preview|simulate","project":{...}}` and returns
HTTP 202 with `{"runId":"...","status":"pending",
"requestedInputHash":"..."}`. A second active run returns HTTP 409 with the
active run ID. Invalid projects return 422. Simulate with no enabled steps returns
422. A run starts from a newly constructed Domain. Preview creates only the initial
geometry and its snapshot. One enabled zero-rate step is a successful no-op and
gets its own independent snapshot.

`GET /api/runs/{id}` returns:

```json
{"runId":"...","kind":"simulate","status":"running",
 "currentStepId":"step_12345678","completedStepIds":[],
 "startedAt":"...","elapsedSeconds":1.2,"requestedInputHash":"...",
 "log":{"cursor":0,"nextCursor":128,"truncated":false,"text":"..."},
 "snapshots":[{"stepId":"initial","file":"initial.vtk"}]}
```

Log cursors are UTF-8 byte offsets in the combined `run.log`. Each response returns
at most 64 KiB and replaces a partial UTF-8 character at a page boundary. The log
collector keeps the first 4 MiB and discards later bytes; `truncated=true` reports
that output cap, not cursor pagination.

`POST /api/runs/{id}/cancel` is idempotent and returns the current status. A terminal
success cannot be changed by a late cancel. `GET /api/runs/{id}/snapshots/{stepId}`
returns one immutable snapshot:

```json
{"schemaVersion":1,"runId":"...","stepId":"initial",
 "axisMap":{"x":0,"h":1,"z":2},"coordinateUnit":"um",
 "points":[[0.0,0.0,0.0]],"lines":[[0,0]],
 "cellData":{"materialIds":[10]},
 "materialIdsByLevelSetIndex":[10],
 "materials":{"10":{"name":"Si","levelSetIndexes":[0],"sourceStepId":null}},
 "mesh":{"vtk":"initial.vtk","pointCount":1,"lineCount":1},
 "noOp":false}
```

The example arrays are schematic; production snapshots contain the actual ViennaPS
mesh. `MaterialIds` is the engine's original cell array. Level-set indexes are
snapshot-local. `sourceStepId` is null where per-cell provenance is not available;
it is never inferred from a repeated material ID.

`GET /api/runs` lists persistent run IDs, kinds, states, hashes, and failure reasons.
`GET /api/runs/{id}/replay-project` returns that run's browser-importable replay
envelope.

`GET /api/runs/{id}/download` is available after a terminal state and streams a ZIP
containing the project and replay project, manifest, combined run log, and each
independently saved VTK/JSON snapshot.
The service includes only an explicit file allowlist and refuses an archive over
the effective artifact cap (`WORKBENCH_MAX_ARTIFACT_MIB`, default 4096 MiB); it
does not stage a second complete archive on disk.

## Error handling and lifecycle states

States are `pending`, `running`, `cancelling`, `succeeded`, `failed`, `timed_out`,
`cancelled`, and `interrupted`. Each failed validation points to a field path. A
worker exception, timeout, output/resource cap, or process cleanup failure cannot
be reported as success. A step failure stops later steps; completed prior snapshots
remain readable.

Responses that contain logs, errors, or geometry use `application/json; charset=utf-8`.
No API accepts Python, shell commands, absolute paths, or arbitrary filenames.
