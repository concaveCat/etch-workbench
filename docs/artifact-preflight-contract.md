# Artifact preflight contract

`backend/preflight.py` estimates whether a normalized project is likely to fill
the per-run artifact cap. The cap is shared by the supervisor, worker, snapshot
staging, and streaming archives and is configured with
`WORKBENCH_MAX_ARTIFACT_MIB` (default **4096 MiB**, minimum 64 MiB, no upper
bound). `backend.runtime_limits` is the single source of truth;
`GET /api/capabilities` reports the effective value, its source (`env` or
`default`), and `artifactLimitMaximumMiB: null`.

## Thresholds

| Constant | Value | Meaning |
|---|---|---|
| `artifact_limit_bytes()` | 4294967296 (4096 MiB) by default | effective hard run-directory cap |
| `WARNING_FRACTION` | 0.75 | allowed, returned with `artifactWarning` |
| `REJECT_FRACTION` | 1.0 | `POST /api/runs` returns 422 before the worker starts |
| `SAFETY_FACTOR` | 1.20 | margin applied to the raw fit |

The defaults are published under `run.artifactPreflight` in
`docs/resource-policy.json`. The static JSON documents the default; the API is
the source of the effective runtime value. A unit test keeps the default in
sync.

## API fields

`GET /api/capabilities` reports the effective limits additively (existing
fields are preserved and now reflect the runtime value):

```json
{
  "runtimeLimits": {
    "artifactLimitBytes": 4294967296,
    "artifactLimitMiB": 4096,
    "artifactLimitSource": "default",
    "artifactLimitEnvironmentVariable": "WORKBENCH_MAX_ARTIFACT_MIB",
    "artifactLimitDefaultMiB": 4096,
    "artifactLimitMinimumMiB": 64,
    "artifactLimitMaximumMiB": null
  },
  "resourcePolicy": {
    "run": {
      "maximumArtifactBytes": 4294967296,
      "maximumResultArchiveBytes": 4294967296,
      "artifactLimitMiB": 4096,
      "artifactPreflight": {"capBytes": 4294967296, "capMiB": 4096}
    }
  }
}
```

`POST /api/projects/validate` (unchanged success contract) adds:

```json
{
  "valid": true,
  "warnings": ["... existing warnings ...", "运行产物预检估算本次运行约 ..."],
  "normalizedProject": {},
  "artifactEstimate": { "...": "..." }
}
```

`POST /api/runs` adds the same `artifactEstimate` object to its 202 response and
an `artifactWarning` string in the warning band. Above the cap it returns 422:

```json
{
  "detail": {
    "code": "artifact-budget-preflight",
    "message": "运行产物预检估算本次运行约 6155 MiB（占 4096 MiB 上限的 150%），已超过上限。...",
    "estimatedBytes": 6453612544,
    "capBytes": 4294967296,
    "suggestions": ["..."],
    "artifactEstimate": { "...": "..." }
  }
}
```

With `WORKBENCH_MAX_ARTIFACT_MIB=512`, the same message reports a 512 MiB cap
and the 684 MiB shape is rejected. `artifactEstimate` contains
`estimatedBytes`, `estimatedMiB`, `utilization`, `capBytes`, `capMiB`,
`risk` (`ok` / `warning` / `reject`), `confidence` (`high` / `medium` / `low`),
`archiveEstimateBytes` (all deferred snapshots materialized, i.e. the run ZIP),
`lazySnapshots`, `contributors` and `suggestions`.

## Formula and calibration

```
cells            = xExtent*yExtent/gridDelta^2   (3D)
                   xExtent/gridDelta             (2D)
stepCount        = 1 (initial) + enabled steps    (0 process steps for preview/slice)
stateBytes       = cells * stateGeometryFactor * layerFactor
                   * sum(160 + 18*i for i in 0..stepCount-1)
initialSnapshot  = 300 * cells * meshGeometryFactor * layerFactor
largestSnapshot  = (300 + 75*weightedSteps) * cells * meshGeometryFactor * layerFactor
optionalOutputs  = outputSettings contribution (volume mesh: 600 bytes/cell/step;
                   level-set exports: intentionally generous, low confidence)
estimatedBytes   = 1.20 * (stateBytes + initialSnapshot + largestSnapshot + optionalOutputs)
archiveBytes     = 1.20 * (stateBytes + allStepSnapshots + optionalOutputs)
```

Geometry factors are deliberately coarse: GDS is the calibrated reference
(`state=1.0`, `mesh=1.0`); a fin uses `state=0.28` (small real feature in a
large lateral domain) and `mesh=2.0` (full-precision mesh over the whole
domain); stack/trench/hole/plane are between those. 2D runs are scaled by 0.4.

Calibration shapes (read-only manifests from completed local runs):

| Shape | On disk | Estimate |
|---|---|---|
| 3D GDS 1x1 um, 0.003 um, 13 steps | 599 MiB | 684 MiB |
| 3D GDS 1x1 um, 0.004 um, 13 steps | 387 MiB | 385 MiB |
| 3D GDS 1x1 um, 0.004 um, 11 steps | 244 MiB | 320 MiB |
| 3D GDS 1x1 um, 0.004 um, 2 steps | 75 MiB | 91 MiB |
| 3D GDS 1x1 um, 0.002 um, preview | 139 MiB | 132 MiB |
| 3D fin 5x5 um, 0.05 um, 7 steps + volume mesh | 108 MiB | 166 MiB |

The first row is the incident that motivated this work: the run itself reached
462 MiB, and materializing its final snapshot took the directory to ~600 MiB.
With the default 4096 MiB cap that shape is allowed (it was rejected under the
legacy 512 MiB cap, `WORKBENCH_MAX_ARTIFACT_MIB=512`). The 0.004 um variant is
well below the cap. A 0.003 um run on the real 0.48 x 0.50 um cell is also far
below the warning threshold. The warning/reject bands are still fractions of
the *effective* cap, so a 4 GiB default warns at 3072 MiB and rejects above
4096 MiB.

## Failure policy

The estimator is not a measurement. If a configuration uses an output mode
without local calibration (`levelSets` / `levelSetMeshes`), `confidence` is
`low` and an over-cap estimate is downgraded to a warning (`risk="warning"`,
`rejectionDowngraded=true`) instead of a false rejection. The user is told that
the estimate is uncertain and over the cap.
