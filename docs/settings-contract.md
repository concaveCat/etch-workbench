# Global settings contract (`GET`/`PUT /api/settings`)

Persistent overrides live in `<data-dir>/settings.json`, which has the shape
`{schemaVersion, limits, defaults}`. The data directory is
`VIENNAPS_WORKBENCH_DATA_DIR` or `<repo>/var`. Writes are atomic (temporary
file + `os.replace`). A `null` limit value removes that override. Files written
before `defaults` existed still load; the missing defaults fall back to the
compiled values.

## Limits and env precedence

| Limit | Kind | Environment boundary | Effective value |
| --- | --- | --- | --- |
| `maxDataGiB` | upper cap | `WORKBENCH_MAX_DATA_GIB` | setting <= env, else env, else default |
| `artifactLimitMiB` | upper cap | `WORKBENCH_MAX_ARTIFACT_MIB` | setting <= env, else env, else default |
| `rssLimitMiB` | upper cap | `WORKBENCH_RSS_LIMIT_MIB` | setting <= env, else env, else default |
| `maxLogMiB` | upper cap | `WORKBENCH_MAX_LOG_MIB` | setting <= env, else env, else default |
| `minFreeGiB` | floor | `WORKBENCH_MIN_FREE_GIB` | setting >= env, else env, else default |

`source` is `settings`, `env`, or `default`. For upper caps `maximum` is the
environment value when set, otherwise `null`; for the floor it is always
`null`. `envValue` is always the raw environment string or `null`.

`runtime_limits.py` re-reads the file (cached by mtime) on every call, so
changes apply without restarting the service. An invalid or unreadable file
falls back to environment/default values with a warning.

## New-project defaults

`defaults.solverTimeoutSeconds` seeds `solver.timeoutSeconds` for newly created
projects. It is a default, not an environment-capped deployment limit: no
`WORKBENCH_*` variable bounds it. Valid values are integers from 5 to 86400
seconds (the project model accepts `0` to mean "no supervisor timeout", and up
to 86400 for an explicit timeout). The compiled default is 600.

## Response

```json
{
  "schemaVersion": 1,
  "limits": {
    "maxDataGiB": {
      "value": 10,
      "source": "default",
      "default": 10,
      "minimum": 1,
      "maximum": null,
      "envVariable": "WORKBENCH_MAX_DATA_GIB",
      "envValue": null,
      "unit": "GiB",
      "label": "数据目录总预算"
    }
  },
  "defaults": {
    "solverTimeoutSeconds": 600
  },
  "storage": {
    "dataDirectory": "/repo/var",
    "usedBytes": 0,
    "freeBytes": 0,
    "totalBytes": 0,
    "runs": 0,
    "projects": 0,
    "domainStates": 0,
    "gdsAssets": 0,
    "csvAssets": 0
  },
  "versions": {"viennaps": "4.7.0", "viennals": "5.10.0"}
}
```

## Validation

`PUT` accepts `{"limits": {<limit>: <integer|null>, ...}}` and
`{"defaults": {<default>: <integer>, ...}}` (partial updates are allowed). A
`null` defaults value is rejected rather than resetting it. Violations return
HTTP 422 with the project-wide error shape:

```json
{"detail": [{"path": "defaults.solverTimeoutSeconds", "message": "..."}]}
```
