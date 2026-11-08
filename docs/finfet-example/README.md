# FinFET.py acceptance fixture

This directory turns the upstream ViennaPS example
`/root/ViennaPS/examples/emulation/FinFET.py` into a platform acceptance test.
It does **not** add a script executor: the example is translated into a
schema-7 project and every operation runs through the ordinary workbench
dispatch, plugin, geometry, state and replay paths.

| File | Purpose |
|---|---|
| `reference/FinFET.py` | verbatim upstream script (SHA-256 `32ec9f2d…`) |
| `reference/FinFET_um.py` | µm twin retained for the scale-sensitivity cross-check |
| `reference/canonical.py` | order-independent surface/volume digest |
| `reference/run_reference.py` | runs a native script and writes `expected*.json` |
| `expected.json` | 26 surfaces + final volume from the original nm script (acceptance reference) |
| `expected-um.json` | µm twin, retained only as the native scale-sensitivity cross-check |
| `project.json` | equivalent schema-7 workbench project (25 steps) |
| `mapping.md` | line-by-line source-to-step and unit mapping |
| `../../tests/acceptance/test_finfet_example.py` | heavy API acceptance test |
| `../../scripts/accept-finfet-example.sh` | one-command acceptance runner |

## What is checked

- 26 native surface checkpoints: point/triangle count, material order,
  per-triangle material distribution and canonical SHA-256.
- Final volume: node/cell count, tetrahedral type, material distribution and
  canonical SHA-256 (`volumeMeshDelta = 0.05`).
- All 25 project steps succeeded through
  `VIENNAPS_WORKBENCH_V2_EXECUTION=1`; `/api/runs/{id}` exposes a `v2Steps`
  record with plan, outputs and state for every step.
- Replay of the same project/seed: final surface and volume digests repeat.
- The default path (v2 switch off) is untouched and covered by the fast
  regression suite.

## Unit frame

The workbench natively supports nanometre schema-7 projects
(`units.length = "nm"`, `units.rate = "nm/s"`). ViennaPS is unit-agnostic, so
the project values reach the native constructors unchanged: no ×0.001
translation is applied to lengths, radii, half-axes, cutoffs or rates.
`parse_project` installs a unit-aware validation context so the historical
0.5 µm grid / 40 µm domain / 2 µm feature limits become their exact
nanometre equivalents without changing any existing µm project. Snapshots and
native records carry `coordinateUnit = "nm"`.

The domain still uses an explicit `domain.bounds` array because the centered
`xExtent`/`yExtent` constructor always produces an odd number of grid points
per axis and cannot reproduce the native 128-point y grid.

The µm twin (`FinFET_um.py` / `expected-um.json`) is kept only to document a
measured ViennaPS limitation: the nm and µm native runs agree on surfaces
0–13 and diverge at surface 14 because level-set mask booleans are not
exactly scale invariant. The acceptance now compares the workbench directly
with the original nm `expected.json`.

## Frontend unit switch

Both editors expose a project length-unit selector:

- WorkflowApp: `01 结构 / 编辑参数 · 计算域 / 网格` → `长度单位`
- LegacyApp: structure panel → `长度单位`

Switching µm→nm (or back) rewrites the project document losslessly: domain,
geometry, step lengths/rates, `volumeMeshDelta`, ray-tracing disk radius and
3D camera/clip positions are scaled by 1000 or 0.001; durations, angles,
direction vectors and orientation multipliers stay unchanged. The unit labels,
domain ranges, viewer axes, scale bar and step summaries follow the declared
unit.

The editor refuses the switch (and keeps the original project) when the
document embeds a frame that cannot be rescaled safely: GDS/Clip geometry,
CSV rate-grid assets, non-selective specialized models, implant/anneal/
analysis/multi-particle steps, or a `continuationState`. Those payloads carry
micrometre-native assets or model-specific units outside the schema.

## Running

Fast checks (no heavy simulation):

```bash
VIENNAPS_WORKBENCH_DATA_DIR=$PWD/var/test-finfet WORKBENCH_PORT=8767 \
  .venv/bin/python -m unittest tests.test_finfet_schema tests.test_finfet_geometry \
  tests.test_finfet_reference -v
```

Full acceptance (native reference twice, workbench project, replay):

```bash
scripts/accept-finfet-example.sh
```

Reuse the committed reference digests and only run the workbench + replay:

```bash
scripts/accept-finfet-example.sh --skip-reference
```

The heavy test is gated by
`VIENNAPS_WORKBENCH_RUN_FINFET_ACCEPTANCE=1`. Runtime artifacts stay below
`var/accept-finfet-*`; the 94 MB final volume is not committed.

## Acceptance evidence

Recorded API acceptance run (native nanometre project):

- run id: `397fe39a3ae0411f247b2150`
- status: succeeded, 25/25 v2 steps
- wall time: 1359.4 s (22 m 39 s), CPU only
- surface 25 digest:
  `6c7f348c412c67f7e87ebf093fff87a3554fecce9b4dbf25a30dbb9f1d0c5084`
- final volume: 2,075,499 nodes / 12,136,389 tetrahedra, digest
  `b5fab9041115584aa338c6aa56e349049526ec55557b970f26c2e1a0e79e974f`
- final volume file SHA-256:
  `a24688213d8fa65b9ff737c73c139648d5a3d3d0703e6f4dbeaa4a1d1e8d9369`
  (byte-identical to the original native volume)
- replay run id: `7bb99976ce4cbae02bfaf98c`
- replay status: succeeded; identical requested/execution hashes
  (`7fd3b590…`, `68f68978…`) and a byte-identical final volume file
  (`a2468821…`). The replay wall time was 3347.0 s because the acceptance was
  intentionally run alongside the regression suite; the first pass took
  1359.4 s.
- snapshots report `coordinateUnit = "nm"` and mesh values are not rescaled.

Known differences: none in the native nm frame. The nm/µm native scale
divergence at surface 14 is retained only as a cross-check, not as the
acceptance target.
